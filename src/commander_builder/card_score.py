"""FP-015 — the unified per-card scoring formula (``CardScore``).

WHAT THIS IS, AND WHAT IT IS NOT
================================
This module computes ONE comparable scalar per card so the four places
that order cards today (advisor adds by bucket-insertion order, advisor
cuts *alphabetically*, ``lift_analysis`` mean-of-top-5 lift, and
``deck_builder_personalize.synergy_scorer``'s unbounded lift sum) can
share a scale.

**It is a ranking prior, not a power rating.** FP-014 states the
project's position — *"assembled decks get Forge-VALIDATED, not just
heuristically scored"* — and FP-002 found that at n=45 **no** pre-sim
deck feature predicts curation margin at ``|t| >= 2``. Both are reasons
to scope this correctly, not reasons to skip it: a scorer can be a
useful *ordering* even when no single feature *regresses* on margin.
The curator/sim stays the arbiter; this formula only decides what gets
simmed first.

Consequences you must respect when consuming this module:

* **Never render a ``CardScore`` to a user as a power rating.** The
  explanation strings this module emits are deliberately written as
  "fits this deck because…" statements, never as card-quality claims.
* **It ships behind a flag, default OFF** — see :func:`is_enabled` and
  ``COMMANDER_BUILDER_CARD_SCORE``. Until the FP-015 tier-3 validation
  (top-k-by-score vs. k-by-bucket-order, both A/B simmed through
  ``compare_versions``) reads positive, the existing bucket-order
  ranking stays the default path.
* **The weights are hand-set priors**, not measured effect sizes.

SHAPE
=====
::

    CardScore(card | deck, commander, bracket, context)
      = Gate(card) * [ 100 * Σ w_k * f_k(card | deck) + Σ m_j(card | deck) ]
      clamped to [0, 100]

Multiplicative gates × weighted additive base × bounded modifiers — the
same structural pattern as ``bracket_estimator.DEFAULT_WEIGHTS``: ONE
documented module-level dict (:data:`CARD_SCORE_WEIGHTS`) is the whole
tuning surface, and every term is explainable in one sentence.

"UNAVAILABLE != BAD" — THE STANDING CONTRACT
============================================
Every signal this module reads can be missing (Scryfall outage, a
corpus under ``lift_analysis.MIN_CORPUS_DECKS``, a commander with no
EDHREC page). A missing component is dropped and the remaining weights
are **renormalized**, never fed a zero — the same contract
``deck_health`` honors by returning ``None`` instead of 0. Feeding a
zero would silently rank "we couldn't measure this" identically to "we
measured this and it's terrible", which are opposite conclusions.
:attr:`CardScore.unavailable` names which components were renormalized
out so a caller can surface which mode is active rather than hide it.

OFFLINE / INJECTION
===================
Everything routes through an injected ``lookup`` (Scryfall-shaped
``name -> dict``), an injected combo list, an injected salt map and an
injected collection, so the whole module runs offline and the test
suite never touches the network. The default lookup is the disk cache
only.

ONE EXCEPTION, worth knowing about: ``role_fit`` calls
``staples.role_target_report``, which resolves names through
``staples.lookup_card`` — a second injection seam this module cannot
reach (see :attr:`DeckContext.role_report` for why we don't fork that
taxonomy to avoid it). A fully-offline caller pins BOTH seams; a
production caller with the flag ON pays a Scryfall resolution per DECK
card on top of the per-candidate ones the advisor already makes. That
cost is one more reason the flag defaults off.

Public API::

    from commander_builder.card_score import (
        CARD_SCORE_WEIGHTS, DeckContext, CardScore, CutScore,
        deck_context, score_card, cut_score, cut_candidates,
        cut_order, is_enabled,
    )

    ctx = deck_context(deck_text, bracket=3, lookup=my_lookup)
    result = score_card("Sol Ring", ctx, inclusion_pct=71.0)
    result.total            # 0..100
    result.components       # {name: Component}
    result.modifiers        # [Modifier, ...]
    result.gates            # [Gate, ...]  (a failure carries its reason)
    result.explanations     # human-readable lines
    result.as_evidence()    # dict for SwapRecommendation.evidence

HONEST LIMITATIONS (copied from the FP-015 plan on purpose)
===========================================================
* It is a prior, not a verdict. Forge remains the arbiter.
* Weights are hand-set until the tier-3 A/B sim reads positive.
* ``role_fit`` inherits ``staples.classify_role``'s regex accuracy;
  a misclassified card is mis-scored and the failure is silent.
* ``synergy`` degrades to the EDHREC term alone under
  ``MIN_CORPUS_DECKS`` harvested decks, so early users get a
  meaningfully more generic ranking. That mode is *reported*
  (``result.unavailable`` / the synergy explanation string), not hidden.
* ``curve_fit`` assumes cast turn == mana value, the same
  simplification ``deck_builder_manabase`` already makes and documents.

MODULE PLACEMENT — this is a core-layer module (sibling of
``bracket_estimator`` / ``combo_detection``). It must never import from
``commander_builder.web``; where a constant lives only in the web layer
(the ``Protect=`` metadata convention, the salt threshold) it is
re-stated here with a pointer, exactly as ``bracket_estimator`` does.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable, Optional

from .lift_analysis import MIN_CORPUS_DECKS
from .staples import (
    POLITICS_SHIELD_REASON,
    ROLE_SATURATION_THRESHOLDS,
    classify_role_extended,
    is_basic_land,
    is_land,
    politics_guard_enabled,
    politics_tags,
    role_target_report,
)

# Scryfall-shaped resolver: ``name -> card dict or None``. The injectable
# seam every derivation here routes through (mirrors
# ``bracket_estimator.CardLookup`` / ``deck_legality.LookupFn``).
CardLookup = Callable[[str], Optional[dict]]


# ---------------------------------------------------------------------------
# The flag. Default OFF.
# ---------------------------------------------------------------------------

#: Env var that opts a run into ``CardScore`` ranking. Truthy values
#: mirror the rest of the codebase (``_advisor_logging.is_enabled``):
#: ``1`` / ``true`` / ``yes``, case-insensitive. Anything else = off.
#:
#: Default OFF is load-bearing, not timidity: FP-014 is explicitly
#: skeptical of static power heuristics and FP-002 found no pre-sim
#: feature predicts curation margin. Until the tier-3 A/B sim reads
#: positive, the shipped default must stay the bucket-order ranking
#: this module is a candidate replacement for.
CARD_SCORE_ENV_VAR = "COMMANDER_BUILDER_CARD_SCORE"


def is_enabled() -> bool:
    """True when the operator has opted into ``CardScore`` ranking.

    Same truthy-value convention as ``_advisor_logging.is_enabled``.
    """
    return os.environ.get(
        CARD_SCORE_ENV_VAR, "",
    ).strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# THE TUNING SURFACE — one documented dict (mirrors bracket_estimator)
# ---------------------------------------------------------------------------

#: Base-component weights. **Sum must be exactly 1.0** (pinned by a
#: test) — the base term is ``100 * Σ w_k * f_k`` with every
#: ``f_k in [0, 1]``, so a weight sum other than 1.0 would silently
#: change the meaning of the 0..100 scale.
#:
#: These encode our priors about Commander deckbuilding, NOT measured
#: effect sizes. The ordering of the priors is the actual claim:
#: "does the deck need this job done" (role_fit) outranks "does it fit
#: THIS deck" (synergy) outranks "does the format agree" (consensus),
#: because a deck-relative signal is what makes this a ranking of
#: candidates *for one deck* rather than a global staple list.
CARD_SCORE_WEIGHTS: dict[str, float] = {
    # Does the format agree this card belongs in this deck? EDHREC
    # inclusion, with Scryfall's edhrec_rank as the offline fallback.
    "consensus": 0.18,
    # Does it fit THIS deck? EDHREC synergy% blended with our own
    # harvested-corpus lift.
    "synergy": 0.24,
    # Does the deck need this job done? The deficit-driven term, and
    # the reason this is deck-relative instead of a power ranking.
    "role_fit": 0.28,
    # Does it fit the curve the archetype wants?
    "curve_fit": 0.16,
    # Does it help meet Frank Karsten's color-source targets?
    "mana_fit": 0.14,
}

# Weight-sum invariant, asserted at import so a typo in the dict above
# can never ship as a quietly-rescaled 0..100 axis.
assert abs(sum(CARD_SCORE_WEIGHTS.values()) - 1.0) < 1e-9, (
    "CARD_SCORE_WEIGHTS must sum to 1.0"
)

#: Modifier magnitudes, on the same 0..100 scale as the base term.
#: Positive entries are bonuses, negative entries are the *maximum*
#: penalty (each is scaled by a clamped ratio, never applied whole
#: unless the trigger is a hard rule).
CARD_SCORE_MODIFIERS: dict[str, float] = {
    "combo_completion": 15.0,   # every other piece already in the deck
    "combo_partial": 6.0,       # 3-card combo, this makes 2 of 3
    "redundancy_relief": 5.0,   # deck has < 3 of an effect this covers
    "owned": 6.0,               # collection.owns() and bias is active
    "price_penalty": -12.0,     # scaled by (usd - soft_cap) / soft_cap
    "salt_penalty": -10.0,      # bracket <= 3 only, scaled over the band
    "bracket_pressure": -20.0,  # tutors / fast mana / extra turns / MLD
    "mdfc_bonus": 3.0,          # modal land = 0.5 land in the health grade
}

# --- consensus -------------------------------------------------------------
# 60% inclusion saturates the term: above that we are measuring "is a
# staple", which role_fit and synergy already handle better.
CONSENSUS_SATURATION_PCT = 60.0
# Offline fallback over Scryfall's edhrec_rank (rank 1 -> 1.0, rank
# ~31600 -> 0.0). log10(31623)/4.5 == 1.0, so the knee sits right about
# where EDHREC's own long tail stops being meaningful.
CONSENSUS_RANK_LOG_DIVISOR = 4.5

# --- synergy ---------------------------------------------------------------
SYNERGY_SATURATION_PCT = 40.0       # EDHREC synergy% that saturates
SYNERGY_EDHREC_SHARE = 0.55         # blend weight for the EDHREC term
SYNERGY_LIFT_SHARE = 0.45           # blend weight for the corpus term
SYNERGY_LIFT_SATURATION = 2.0       # lift 3.0 (== 1.0 + 2.0) saturates

# --- curve_fit -------------------------------------------------------------
# Hand-set NONLAND curve prior, in card slots for a 99-card deck with a
# ~37-land manabase (62 nonland slots). Bucket 7 is "7+" — the same
# ``int(min(cmc, 7))`` bucketing the FP-015 spec writes.
#
# This is a prior, not a measurement: it is the shape a "normal"
# midrange Commander deck is usually taught to have. ``archetype_curve``
# tilts it per archetype/bracket.
CURVE_NONLAND_SLOTS = 62
TARGET_CURVE_BASE: dict[int, float] = {
    0: 2.0, 1: 8.0, 2: 12.0, 3: 12.0, 4: 10.0, 5: 7.0, 6: 5.0, 7: 6.0,
}
# Mana value the tilt pivots around (buckets below it gain when the tilt
# is negative, buckets above it lose).
CURVE_TILT_PIVOT = 3.0
#: Per-archetype curve tilt. Negative shifts the target curve DOWN (more
#: cheap cards) — aggro wants to deploy, combo wants to assemble early;
#: positive shifts it UP — control trades its early turns for bigger
#: late ones. This is where ``archetype.classify`` finally touches card
#: selection: today it feeds only pod diversity and a bracket nudge.
ARCHETYPE_CURVE_TILT: dict[str, float] = {
    "aggro": -0.50,
    "combo": -0.50,
    "midrange": 0.0,
    "control": 0.35,
    "stax": -0.15,
}
# Higher brackets run cheaper; lower brackets run heavier. Same
# direction as bracket_estimator's ``curve_tight`` / ``curve_high``
# signals, which read <= 2.6 avg CMC as "tuned" and >= 3.8 as
# "battlecruiser".
BRACKET_CURVE_TILT: dict[int, float] = {1: 0.15, 2: 0.10, 3: 0.0,
                                        4: -0.15, 5: -0.25}

# --- bracket_pressure ------------------------------------------------------
#: Game Changer cap per bracket, from WotC's bracket table: B1/B2 allow
#: none, B3 allows 3, B4/B5 are unrestricted (``None`` == no cap). This
#: is the ``bracket_cap`` GATE's table, not a modifier.
GAME_CHANGER_CAP: dict[int, Optional[int]] = {1: 0, 2: 0, 3: 3,
                                              4: None, 5: None}
#: How many tutors a bracket tolerates before an extra one reads as
#: bracket pressure. "Tutors should be sparse" at B1/B2 per the WotC
#: guidance the audit prompt encodes.
TUTOR_BUDGET: dict[int, Optional[int]] = {1: 1, 2: 1, 3: 3,
                                          4: None, 5: None}
#: Same, for non-Game-Changer fast mana.
FAST_MANA_BUDGET: dict[int, Optional[int]] = {1: 0, 2: 0, 3: 2,
                                              4: None, 5: None}
# Per-trigger bracket_pressure charges. The sum is clamped to the
# CARD_SCORE_MODIFIERS["bracket_pressure"] floor (-20).
_PRESSURE_TUTOR = -8.0
_PRESSURE_FAST_MANA = -6.0
_PRESSURE_EXTRA_TURN = -10.0
_PRESSURE_MLD = -20.0        # WotC prohibits mass land denial at B1-B3.

# --- salt ------------------------------------------------------------------
# Mirrors ``web/deck_insights._SALT_WARN_THRESHOLD`` (1.5 on EDHREC's
# 0..5 scale) and ``bracket_estimator._SALT_THRESHOLD``. Redefined here
# for the same reason those two are: core modules must not import from
# the web layer.
SALT_THRESHOLD = 1.5
SALT_SATURATION_BAND = 2.5   # salt 4.0 (== 1.5 + 2.5) takes the full -10
SALT_MAX_BRACKET = 3         # salt only reads as a mismatch at B <= 3

# --- price -----------------------------------------------------------------
# Default soft cap when the caller has no budget setting. A card at 2x
# the cap takes the full penalty.
DEFAULT_PRICE_SOFT_CAP = 25.0

# --- redundancy ------------------------------------------------------------
# "Deck has < 3 instances of an effect this card duplicates." Uses
# ``interaction.INTERACTION_CATEGORIES`` as the effect taxonomy.
REDUNDANCY_THRESHOLD = 3

# --- cut guard rails -------------------------------------------------------
#: Effective-land floor. Mirrors ``deck_health._LAND_BAND[0]``: MDFC
#: spell fronts count 0.5 lands each, and 33 is the bottom of the band
#: an MDFC-heavy deck is allowed to reach.
EFFECTIVE_LAND_FLOOR = 33.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    """One multiplicative 0/1 gate. A failure carries its reason.

    Gates are reported, never silently zeroed: "this card is banned"
    and "this card scored badly" must not render identically.
    """
    name: str
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"gate": self.name, "passed": self.passed,
                "reason": self.reason}


@dataclass(frozen=True)
class Component:
    """One base component ``f_k``.

    ``value is None`` means UNAVAILABLE — the component is dropped and
    the surviving weights are renormalized. It never contributes a zero.
    ``effective_weight`` is the post-renormalization weight actually
    applied (0.0 for an unavailable component).
    """
    name: str
    value: Optional[float]
    weight: float
    explanation: str
    effective_weight: float = 0.0

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def points(self) -> float:
        """Points this component contributed to the 0..100 base term."""
        if self.value is None:
            return 0.0
        return 100.0 * self.effective_weight * self.value

    def to_dict(self) -> dict:
        return {"component": self.name, "value": self.value,
                "weight": self.weight,
                "effective_weight": round(self.effective_weight, 4),
                "points": round(self.points, 2),
                "explanation": self.explanation}


@dataclass(frozen=True)
class Modifier:
    """One bounded additive adjustment on the 0..100 scale."""
    name: str
    points: float
    explanation: str

    def to_dict(self) -> dict:
        return {"modifier": self.name, "points": round(self.points, 2),
                "explanation": self.explanation}


@dataclass(frozen=True)
class CardScore:
    """Structured result: total, sub-scores, modifiers, gates, prose.

    ``as_evidence()`` shapes this for ``SwapRecommendation.evidence``,
    which already flows to the UI (``_advisor_heuristic.py`` lines
    383-389), so the breakdown surfaces with no schema change.
    """
    card: str
    total: float
    base: float
    components: dict[str, Component]
    modifiers: tuple[Modifier, ...] = ()
    gates: tuple[Gate, ...] = ()

    @property
    def gated(self) -> bool:
        """True when at least one gate zeroed this card."""
        return any(not g.passed for g in self.gates)

    @property
    def gate_reasons(self) -> list[str]:
        return [g.reason for g in self.gates if not g.passed]

    @property
    def unavailable(self) -> list[str]:
        """Components that were renormalized out (never zeroed)."""
        return [n for n, c in self.components.items() if not c.available]

    @property
    def explanations(self) -> list[str]:
        """Human-readable lines. Deliberately phrased as fit statements,
        never as card-quality or power claims."""
        out: list[str] = []
        for g in self.gates:
            if not g.passed:
                out.append(g.reason)
        if self.gated:
            return out
        for name in CARD_SCORE_WEIGHTS:
            comp = self.components.get(name)
            if comp is not None:
                out.append(comp.explanation)
        out.extend(m.explanation for m in self.modifiers)
        return out

    def as_evidence(self) -> dict:
        """Dict shaped for ``SwapRecommendation.evidence["card_score"]``."""
        return {
            "total": round(self.total, 2),
            "base": round(self.base, 2),
            "gated": self.gated,
            "gates": [g.to_dict() for g in self.gates if not g.passed],
            "components": {n: c.to_dict()
                           for n, c in self.components.items()},
            "modifiers": [m.to_dict() for m in self.modifiers],
            "unavailable": self.unavailable,
            "explanations": self.explanations,
            # Consumers must not relabel this as a power/quality rating.
            "kind": "ranking_prior",
        }


@dataclass(frozen=True)
class CutScore:
    """``100 - CardScore(card | deck_without_card)`` plus guard rails.

    ``blocked`` is True when a guard rail forbids cutting this card at
    all; ``block_reason`` says which one. A blocked card is never a cut
    candidate regardless of its score.
    """
    card: str
    score: float
    card_score: CardScore
    blocked: bool = False
    block_reason: str = ""

    @property
    def explanations(self) -> list[str]:
        if self.blocked:
            return [self.block_reason]
        return self.card_score.explanations

    def to_dict(self) -> dict:
        return {"card": self.card, "cut_score": round(self.score, 2),
                "blocked": self.blocked, "block_reason": self.block_reason,
                "card_score": self.card_score.as_evidence()}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _key(name: str) -> str:
    return (name or "").strip().lower()


def _default_lookup(name: str) -> Optional[dict]:
    """Disk-cache-only Scryfall read. NEVER touches the network.

    Scoring runs inside ranking loops over a hundred candidates; a
    per-card network timeout there would be unacceptable, and the
    advisor's own ``_cached_scryfall`` makes the same choice.
    """
    try:
        from .scryfall_client import _cache_path
        path = _cache_path(name)
    except Exception:  # noqa: BLE001
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _type_line(card: Optional[dict]) -> str:
    """Full type line, faces joined.

    Matches ``deck_legality._type_line``.
    """
    if not card:
        return ""
    tl = card.get("type_line") or ""
    if tl:
        return tl
    faces = card.get("card_faces") or []
    return " // ".join((f or {}).get("type_line") or "" for f in faces)


def _oracle_text(card: Optional[dict]) -> str:
    if not card:
        return ""
    txt = card.get("oracle_text") or ""
    if txt:
        return txt
    faces = card.get("card_faces") or []
    return "\n".join((f or {}).get("oracle_text") or "" for f in faces)


def _cmc(card: Optional[dict]) -> Optional[float]:
    if not card:
        return None
    value = card.get("cmc")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_land_card(name: str, card: Optional[dict]) -> bool:
    """Front-face land test. Falls back to the curated name list.

    FRONT face decides, matching ``deck_health``'s and
    ``manabase_report``'s land walks: a "Sorcery // Land" MDFC is a
    spell you sometimes play as a land, not a land.
    """
    tl = _type_line(card)
    if tl:
        return "land" in tl.split("//")[0].lower()
    return is_land(name)


# ``[metadata] Protect=`` lines. The convention (one card per line,
# comma is literal) is defined by ``web/_helpers.read_protected_cards``;
# it is re-stated here rather than imported because core modules must
# not import from the web layer (same reason bracket_estimator restates
# the salt threshold).
_PROTECT_RE = re.compile(r"^\s*Protect\s*=\s*(.+?)\s*$", re.IGNORECASE)


def protected_from_deck_text(deck_text: str) -> list[str]:
    """``[metadata] Protect=`` entries — the cards a user locked
    against cuts."""
    out: list[str] = []
    for raw in (deck_text or "").splitlines():
        m = _PROTECT_RE.match(raw)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def synth_deck_text(commander_names: Iterable[str],
                    deck_cards: Iterable[str]) -> str:
    """Render a minimal ``.dck`` blob from name lists.

    Every shipped deck-level helper this module consumes
    (``manabase_report``, ``one_piece_away``, ``interaction_report``)
    takes ``deck_text``. Callers that only hold a name set (the
    advisor's ``deck_cards``) get an equivalent blob here instead of
    those helpers growing a second entry point.
    """
    lines = ["[metadata]", "[Commander]"]
    lines += [f"1 {n}" for n in commander_names if n]
    lines.append("[Main]")
    lines += [f"1 {n}" for n in deck_cards if n]
    return "\n".join(lines) + "\n"


def _deck_text_minus(deck_text: str, card_name: str) -> str:
    """``deck_text`` with every ``[Main]`` line for ``card_name`` removed.

    :meth:`DeckContext.without` uses this so a child context keeps its
    parent's REAL deck text — a stacked ``27 Mountain`` line stays 27
    Mountains in the child's manabase math. Re-synthesizing from the
    remaining name list (the pre-fix behavior) collapsed every quantity
    to 1x, so the child's Karsten source counts saw a ~8-land deck.
    Matches ``without``'s name semantics: ALL lines for the name go.
    """
    from .dck_utils import parse_card_line
    k = _key(card_name)
    out: list[str] = []
    in_main = False
    for raw in (deck_text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
        elif in_main:
            parsed = parse_card_line(stripped)
            if parsed and parsed[1] and _key(parsed[1]) == k:
                continue
        out.append(raw)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Deck context — every deck-level derivation, memoized and injectable
# ---------------------------------------------------------------------------

class DeckContext:
    """Everything ``score_card`` needs to know about the deck.

    Deck-level derivations (role report, manabase report, MV histogram,
    interaction coverage, combo membership, Game-Changer / tutor / fast
    mana counts) are computed lazily and memoized, so scoring 200
    candidates against one deck derives each exactly once.

    Build one with :func:`deck_context`.
    """

    def __init__(
        self,
        *,
        deck_text: str = "",
        deck_cards: Optional[Iterable[str]] = None,
        commander_names: Optional[Iterable[str]] = None,
        bracket: Optional[int] = None,
        archetype: Optional[str] = None,
        lookup: Optional[CardLookup] = None,
        combos: Optional[list[dict]] = None,
        corpus_decks: int = 0,
        lift_scores: Optional[dict[str, float]] = None,
        collection_keys: Optional[frozenset[str]] = None,
        collection_bias: bool = False,
        price_soft_cap: Optional[float] = None,
        salt_scores: Optional[dict[str, float]] = None,
        game_changers: Optional[Iterable[str]] = None,
        protected_cards: Optional[Iterable[str]] = None,
        politics_guard: Optional[bool] = None,
        use_forge: bool = False,
    ) -> None:
        self._resolve = lookup or _default_lookup
        self._cache: dict[str, Optional[dict]] = {}

        if deck_cards is None or commander_names is None:
            parsed_cmd, parsed_main = _split_deck_text(deck_text)
            if commander_names is None:
                commander_names = parsed_cmd
            if deck_cards is None:
                deck_cards = parsed_main

        self.commander_names: tuple[str, ...] = tuple(
            n for n in (commander_names or ()) if n
        )
        self.deck_cards: tuple[str, ...] = tuple(
            n for n in (deck_cards or ()) if n
        )
        self.deck_keys: frozenset[str] = frozenset(
            _key(n) for n in self.deck_cards
        )
        self.deck_text: str = deck_text or synth_deck_text(
            self.commander_names, self.deck_cards,
        )
        self.bracket = bracket
        self.archetype = archetype
        self.combos = combos
        self.corpus_decks = int(corpus_decks or 0)
        self.lift_scores = {
            _key(k): float(v) for k, v in (lift_scores or {}).items()
        }
        self.collection_keys = collection_keys
        self.collection_bias = bool(collection_bias)
        self.price_soft_cap = (
            float(price_soft_cap) if price_soft_cap else None
        )
        self._salt_scores = (
            {_key(k): float(v) for k, v in salt_scores.items()}
            if salt_scores is not None else None
        )
        self._game_changers = (
            frozenset(_key(n) for n in game_changers)
            if game_changers is not None else None
        )
        self.protected_keys: frozenset[str] = frozenset(
            _key(n) for n in (
                protected_cards
                if protected_cards is not None
                else protected_from_deck_text(deck_text)
            )
        )
        # Politics guard (decision C2). Derived from the deck's
        # ``[metadata] PoliticsGuard=`` line when the caller doesn't state
        # it, exactly as ``protected_keys`` derives ``Protect=``. Resolved
        # ONCE here (not per cut candidate) so ``without()``'s children
        # inherit the answer instead of re-parsing the blob 99 times.
        self.politics_guard: bool = (
            bool(politics_guard) if politics_guard is not None
            else politics_guard_enabled(deck_text)
        )
        self.use_forge = bool(use_forge)

        # Memo slots. A key present with a ``None`` value means
        # "computed, and legitimately unavailable" (the outage
        # contract) — distinct from "not computed yet" (key absent).
        self._memo: dict[str, object] = {}
        # Set by :meth:`without` so a derived context can subtract one
        # card from its parent's role report instead of re-walking.
        self._parent_report: Optional[dict] = None
        self._removed_card: Optional[str] = None
        self._removed_copies: int = 0

        self._init_kwargs = dict(
            bracket=bracket, archetype=archetype, lookup=self._resolve,
            combos=combos, corpus_decks=corpus_decks,
            lift_scores=lift_scores, collection_keys=collection_keys,
            collection_bias=collection_bias, price_soft_cap=price_soft_cap,
            salt_scores=salt_scores, game_changers=game_changers,
            protected_cards=tuple(
                protected_cards
                if protected_cards is not None
                else protected_from_deck_text(deck_text)
            ),
            politics_guard=self.politics_guard,
            use_forge=use_forge,
        )

    # --- lookups ----------------------------------------------------------

    def card(self, name: str) -> Optional[dict]:
        """One memoized, never-raising lookup per distinct name."""
        k = _key(name)
        if k not in self._cache:
            try:
                self._cache[k] = self._resolve(name)
            except Exception:  # noqa: BLE001 — a blip must not crash ranking
                self._cache[k] = None
        return self._cache[k]

    def without(self, card_name: str) -> "DeckContext":
        """A context for the deck MINUS ``card_name``.

        This is what makes cut scoring honest: a card in a saturated
        role must be scored against the deck that does *not* already
        contain it, or the saturation term charges it for its own
        presence and every member of the role looks equally bad.
        """
        k = _key(card_name)
        remaining = [n for n in self.deck_cards if _key(n) != k]
        # The child inherits the parent's deck TEXT minus the card's
        # line(s), not a re-synthesized 1x blob — text-derived math
        # (manabase, effective_lands) must keep real quantities.
        ctx = DeckContext(
            deck_text=_deck_text_minus(self.deck_text, card_name),
            deck_cards=remaining,
            commander_names=self.commander_names,
            **self._init_kwargs,
        )
        # Share the resolved-card memo: the deck differs by one name, the
        # Scryfall answers do not.
        ctx._cache = self._cache
        # Let the child derive its role report from ours by subtracting
        # the removed card, instead of re-walking the whole deck. Cut
        # ordering builds one child per card, so a fresh walk each time
        # would be a quadratic number of ``staples.lookup_card`` calls —
        # cheap on a warm disk cache, but an unresolvable name is not
        # cached and would be re-fetched once per child.
        ctx._parent_report = self.role_report
        ctx._removed_card = card_name
        # Commander is singleton so this is normally 1, but a caller can
        # hand us a list with repeats (basics, or a fixture) and the
        # subtraction has to match what was actually removed.
        ctx._removed_copies = sum(
            1 for n in self.deck_cards if _key(n) == k
        )
        return ctx

    # --- deck-level derivations (lazy, memoized) --------------------------

    def _lazy(self, key: str, fn):
        if key not in self._memo:
            try:
                self._memo[key] = fn()
            except Exception:  # noqa: BLE001 — scoring must never raise
                self._memo[key] = None
        return self._memo[key]

    @property
    def color_identity(self) -> Optional[frozenset[str]]:
        """The commander's color identity, or None when unresolvable.

        None means "we could not verify", which the ``color_identity``
        gate treats as pass-with-a-note — never as a violation.
        """
        def _compute():
            if not self.commander_names:
                return None
            out: set[str] = set()
            resolved = False
            for name in self.commander_names:
                card = self.card(name)
                if card is None:
                    continue
                resolved = True
                out |= {
                    c.upper() for c in (card.get("color_identity") or [])
                    if isinstance(c, str)
                }
            return frozenset(out) if resolved else None
        return self._lazy("color_identity", _compute)

    @property
    def role_report(self) -> Optional[dict]:
        """``staples.role_target_report`` over the deck, WITH commander
        credit (a commander that fills a role reduces that role's
        target — Edric should not demand 10 draw spells).

        NOTE ON THE LOOKUP SEAM: ``role_target_report`` resolves card
        names through ``staples.lookup_card``, a SEPARATE injection
        point from this context's ``lookup`` (that import is
        module-level in ``staples`` specifically so it can be
        monkeypatched — see the comment on it). We deliberately do not
        fork ``count_deck_roles``' taxonomy to route it through our own
        lookup: the FP-015 plan is explicit that the role taxonomy must
        keep exactly one definition, and a second copy of the
        ``win_condition`` promotion rule would drift. Fully-offline
        callers should pin both seams.

        A context produced by :meth:`without` SUBTRACTS the removed card
        from its parent's report rather than re-walking the deck. That
        is exact, not an approximation: ``count_deck_roles`` is a plain
        per-card counter, so classifying the one removed card with the
        same function and decrementing its bucket reproduces the full
        walk — and it keeps cut ordering linear in deck size.
        """
        def _compute():
            if self._parent_report is not None and self._removed_card:
                derived = _role_report_minus(
                    self._parent_report, self._removed_card, self,
                    copies=self._removed_copies,
                )
                if derived is not None:
                    return derived
            return role_target_report(
                list(self.deck_cards), list(self.commander_names),
            )
        return self._lazy("role_report", _compute)

    @property
    def manabase(self) -> Optional[dict]:
        """``deck_builder_manabase.manabase_report`` over this deck."""
        def _compute():
            from .deck_builder_manabase import manabase_report
            return manabase_report(self.deck_text, lookup=self.card)
        return self._lazy("manabase", _compute)

    @property
    def curve(self) -> dict[int, int]:
        """Actual NONLAND mana-value histogram, bucketed ``min(cmc, 7)``.

        Nothing in the repo computed this before FP-015; ``curve_fit``
        is its first consumer.
        """
        def _compute():
            hist = {mv: 0 for mv in TARGET_CURVE_BASE}
            for name in self.deck_cards:
                card = self.card(name)
                if _is_land_card(name, card):
                    continue
                mv = _cmc(card)
                if mv is None:
                    continue
                hist[int(min(max(mv, 0.0), 7.0))] += 1
            return hist
        return self._lazy("curve", _compute) or {}

    @property
    def interaction(self) -> Optional[dict]:
        """``interaction.interaction_report`` coverage matrix, or None."""
        def _compute():
            from .interaction import interaction_report
            return interaction_report(
                self.deck_text, bracket=self.bracket, lookup=self.card,
                use_forge=self.use_forge,
            )
        return self._lazy("interaction", _compute)

    @property
    def combo_pool(self) -> list[dict]:
        def _compute():
            if self.combos is not None:
                return self.combos
            from .combo_detection import load_combos
            return load_combos()
        return self._lazy("combo_pool", _compute) or []

    @property
    def one_piece_away(self) -> list[dict]:
        """Combos this deck is EXACTLY one card short of.

        The ``combo_completion`` modifier's whole input. Before FP-015 a
        candidate that completed a half-assembled combo got exactly zero
        boost — combo membership was never consulted during ranking.
        """
        def _compute():
            from .combo_detection import one_piece_away
            return one_piece_away(
                self.deck_text, combos=self.combos, lookup=self.card,
            )
        return self._lazy("one_piece_away", _compute) or []

    @property
    def combo_pieces(self) -> frozenset[str]:
        """Keys of in-deck cards belonging to a FULLY detected combo.

        The cut guard rail "never cut a piece of a detected in-deck
        combo" reads this.
        """
        def _compute():
            from .combo_detection import detect_combos_in_deck
            found = detect_combos_in_deck(self.deck_text,
                                          combos=self.combos)
            keys: set[str] = set()
            for combo in found:
                for name in combo.get("cards") or []:
                    if _key(name) in self.deck_keys:
                        keys.add(_key(name))
            return frozenset(keys)
        return self._lazy("combo_pieces", _compute) or frozenset()

    @property
    def salt_scores(self) -> Optional[dict[str, float]]:
        """EDHREC salt map from the DISK CACHE ONLY, or None.

        None reads as "salt unavailable" (no penalty), never as
        "salt 0" — the same choice ``bracket_estimator._offline_salt_count``
        makes.
        """
        if self._salt_scores is not None:
            return self._salt_scores

        def _compute():
            from .edhrec_client import CACHE_DIR
            path = CACHE_DIR.parent / "edhrec_salt" / "top-salt.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                return None
            return {_key(k): float(v or 0) for k, v in data.items()}
        return self._lazy("salt_scores", _compute)

    def is_game_changer(self, name: str) -> bool:
        if self._game_changers is not None:
            return _key(name) in self._game_changers

        def _compute():
            from .game_changers import load_game_changers
            return frozenset(_key(n) for n in load_game_changers())
        loaded = self._lazy("game_changers", _compute) or frozenset()
        return _key(name) in loaded

    @property
    def game_changer_count(self) -> int:
        return sum(1 for n in self.deck_cards if self.is_game_changer(n))

    def politics_tags_of(self, name: str) -> tuple[str, ...]:
        """Politics mechanics on ``name`` (goad / monarch / vote / ...).

        Routes through :meth:`card` — the injected, memoized, never-raising
        lookup — so this stays on the module's offline contract rather than
        calling ``staples``' own Scryfall-backed name resolver.
        """
        card = self.card(name)
        if not card:
            return ()
        return politics_tags(card.get("oracle_text", "") or "",
                             card.get("type_line", "") or "")

    @property
    def tutor_count(self) -> int:
        def _compute():
            from .bracket_estimator import _TUTOR_CARDS
            return sum(1 for n in self.deck_cards if _key(n) in _TUTOR_CARDS)
        return self._lazy("tutor_count", _compute) or 0

    @property
    def fast_mana_count(self) -> int:
        def _compute():
            from .bracket_estimator import _FAST_MANA_CARDS
            return sum(1 for n in self.deck_cards
                       if _key(n) in _FAST_MANA_CARDS)
        return self._lazy("fast_mana_count", _compute) or 0

    @property
    def extra_turn_count(self) -> int:
        def _compute():
            from .bracket_estimator import _EXTRA_TURN_CARDS
            return sum(1 for n in self.deck_cards
                       if _key(n) in _EXTRA_TURN_CARDS)
        return self._lazy("extra_turn_count", _compute) or 0

    @property
    def effective_lands(self) -> float:
        """Lands + 0.5 per spell-front MDFC — ``deck_health``'s counting.

        Quantity-aware: walks ``self.deck_text`` (the real ``.dck`` blob
        when the caller supplied one, the synthesized 1x blob otherwise),
        so a stacked ``27 Mountain`` line counts as 27 lands rather than
        the 1 the old per-entry walk over ``deck_cards`` credited.
        """
        def _compute():
            from .dck_utils import iter_main_cards
            from .deck_health import _MDFC_LANDS
            total = 0.0
            for qty, name in iter_main_cards(self.deck_text):
                if _is_land_card(name, self.card(name)):
                    total += float(qty)
                elif _key(name) in _MDFC_LANDS:
                    total += 0.5 * float(qty)
            return total
        value = self._lazy("effective_lands", _compute)
        return float(value or 0.0)

    def is_mdfc_land(self, name: str) -> bool:
        def _compute():
            from .deck_health import _MDFC_LANDS
            return _MDFC_LANDS
        pool = self._lazy("mdfc_lands", _compute) or frozenset()
        return _key(name) in pool

    def role_of(self, name: str) -> str:
        """Extended role label, or ``"unknown"`` on a lookup miss."""
        card = self.card(name)
        if not card:
            return "unknown"
        return classify_role_extended(_oracle_text(card), _type_line(card))

    def role_bucket_of(self, name: str) -> str:
        """``staples.role_bucket`` via THIS context's injected lookup.

        The count_deck_roles taxonomy (base roles + the one-way wincon
        promotion) without count_deck_roles' module-level
        ``staples.lookup_card`` — the seam violation OPTIMIZATION_AUDIT
        P3 documents. Unknown cards bucket into ``"other"``, matching
        count_deck_roles' defensive contract.
        """
        card = self.card(name)
        if not card:
            return "other"
        from .staples import role_bucket
        return role_bucket(_oracle_text(card), _type_line(card))

    @property
    def deck_role_counts(self) -> "Counter":
        """Per-role bucket counts over the mainboard, derived once.

        Replaces the full 99-name ``count_deck_roles`` recount that
        ``_role_target_for``'s tutor fallback used to run PER SCORED
        CANDIDATE (and which routed through the module-level lookup).
        """
        def _compute():
            from collections import Counter
            return Counter(self.role_bucket_of(n) for n in self.deck_cards)
        return self._lazy("deck_role_counts", _compute)


def _role_report_minus(report: dict, card_name: str, ctx: "DeckContext",
                       *, copies: int = 1) -> Optional[dict]:
    """``report`` recomputed as if ``card_name`` were not in the deck.

    Classifies the one removed card via ``ctx.role_bucket_of`` — the
    count_deck_roles taxonomy through the context's INJECTED lookup
    (OPTIMIZATION_AUDIT P3) — and decrements its bucket,
    folding ``win_condition`` into ``finisher`` exactly as
    ``role_target_report`` does. Returns None if the shape is not what
    we expect, so the caller falls back to a full recount.
    """
    roles = report.get("roles")
    if not isinstance(roles, dict):
        return None
    # ctx-injected lookup ONLY — count_deck_roles would route through
    # staples.lookup_card (live HTTP on a cold cache, per cut candidate;
    # OPTIMIZATION_AUDIT P3). Same taxonomy via staples.role_bucket.
    try:
        bucket = ctx.role_bucket_of(card_name)
    except Exception:  # noqa: BLE001
        return None
    if bucket == "win_condition":
        bucket = "finisher"
    out: dict[str, dict] = {}
    for role, entry in roles.items():
        count = int(entry.get("count", 0))
        if role == bucket:
            count = max(0, count - max(1, copies))
        target = int(entry.get("target", 0))
        out[role] = {**entry, "count": count,
                     "deficit": max(0, target - count)}
    under = sorted((r for r, v in out.items() if v["deficit"] > 0),
                   key=lambda r: out[r]["deficit"], reverse=True)
    return {"roles": out, "under_built": under}


def _split_deck_text(deck_text: str) -> tuple[list[str], list[str]]:
    """``(commander_names, main_names)`` from a ``.dck`` blob."""
    if not deck_text:
        return [], []
    try:
        from .dck_utils import section_card_names
        return (list(section_card_names(deck_text, "Commander")),
                list(section_card_names(deck_text, "Main")))
    except Exception:  # noqa: BLE001
        return [], []


def deck_context(deck_text: str = "", **kwargs) -> DeckContext:
    """Build a :class:`DeckContext`. See that class for the keywords."""
    return DeckContext(deck_text=deck_text, **kwargs)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def _gate_legal(name: str, ctx: DeckContext) -> Gate:
    """``legalities.commander`` via ``deck_legality.scan_banned``.

    Scryfall-backed, NOT a hardcoded ban set: the hand-typed set this
    replaces had drifted six cards in one direction and ten in the other
    within a single B&R cycle. An unresolvable name is *unverified*, so
    the gate PASSES with a note — refusing to rank a card because
    Scryfall is down is the wrong failure.
    """
    from .deck_legality import scan_banned
    try:
        scan = scan_banned([name], lookup=ctx.card)
    except Exception:  # noqa: BLE001
        scan = None
    if scan is None:
        return Gate("legal", True,
                    "legality unverified (Scryfall unavailable)")
    if scan.banned:
        return Gate("legal", False,
                    f"{name} is banned in Commander")
    if scan.not_in_format:
        return Gate("legal", False,
                    f"{name} is not legal in Commander")
    return Gate("legal", True, "")


def _gate_color_identity(name: str, ctx: DeckContext) -> Gate:
    identity = ctx.color_identity
    if identity is None:
        return Gate("color_identity", True,
                    "color identity unverified (no resolvable commander)")
    card = ctx.card(name)
    if card is None:
        return Gate("color_identity", True,
                    f"color identity unverified for {name}")
    card_ci = {c.upper() for c in (card.get("color_identity") or [])
               if isinstance(c, str)}
    extra = sorted(card_ci - set(identity))
    if extra:
        return Gate("color_identity", False,
                    f"{name} is outside the commander's color identity "
                    f"({'/'.join(extra)} not in "
                    f"{'/'.join(sorted(identity)) or 'colorless'})")
    return Gate("color_identity", True, "")


def _gate_singleton(name: str, ctx: DeckContext) -> Gate:
    if _key(name) in ctx.deck_keys and not is_basic_land(name):
        return Gate("singleton", False,
                    f"{name} is already in the deck (Commander is singleton)")
    return Gate("singleton", True, "")


def _gate_bracket_cap(name: str, ctx: DeckContext) -> Gate:
    """Would adding this card exceed the bracket's Game Changer cap?"""
    if ctx.bracket is None:
        return Gate("bracket_cap", True, "")
    cap = GAME_CHANGER_CAP.get(int(ctx.bracket))
    if cap is None:
        return Gate("bracket_cap", True, "")
    if not ctx.is_game_changer(name):
        return Gate("bracket_cap", True, "")
    if _key(name) in ctx.deck_keys:
        # Already counted in the deck total — scoring it in place (cut
        # scoring) must not charge it for its own presence.
        return Gate("bracket_cap", True, "")
    if ctx.game_changer_count >= cap:
        return Gate(
            "bracket_cap", False,
            f"{name} is a Game Changer and bracket {ctx.bracket} allows "
            f"{cap} (deck already has {ctx.game_changer_count})",
        )
    return Gate("bracket_cap", True, "")


def _gates(name: str, ctx: DeckContext) -> tuple[Gate, ...]:
    return (
        _gate_legal(name, ctx),
        _gate_color_identity(name, ctx),
        _gate_singleton(name, ctx),
        _gate_bracket_cap(name, ctx),
    )


# ---------------------------------------------------------------------------
# Base components
# ---------------------------------------------------------------------------

def _f_consensus(
    name: str, ctx: DeckContext, inclusion_pct: Optional[float],
) -> tuple[Optional[float], str]:
    """Does the format agree this card belongs here?

    ``inclusion_pct / 60`` saturated at 1.0. EDHREC sometimes reports a
    raw DECK COUNT in this field (30627 == "in 30627 decks", a quirk the
    advisor's rationale strings already special-case at ``<= 100``); a
    value above 100 therefore is NOT a percentage and we fall through to
    the ``edhrec_rank`` path rather than pretend every such card is a
    perfect 1.0.
    """
    # ``0.0 <`` is deliberate — the opposite choice from ``_f_synergy``.
    # A literal 0.0 inclusion is overwhelmingly a MISSING-data sentinel,
    # not a measurement: ``edhrec_client.CardEntry`` defaults the field
    # to 0.0 and its parsers coerce absent values with ``or 0``, and a
    # card genuinely recommended on an EDHREC commander page never
    # carries a true 0% inclusion. Falling through swaps that ambiguous
    # sentinel for ``edhrec_rank`` — a real per-card measurement — so
    # this is "unavailable != bad" honored, not violated. (Synergy has
    # no such fallback signal, and its 0.0 IS a common true value.)
    if inclusion_pct is not None and 0.0 < inclusion_pct <= 100.0:
        f = _clamp(inclusion_pct / CONSENSUS_SATURATION_PCT)
        return f, (f"in {inclusion_pct:.0f}% of EDHREC decks for this "
                   f"commander")
    card = ctx.card(name)
    rank = (card or {}).get("edhrec_rank")
    try:
        rank = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None
    if rank and rank > 0:
        f = _clamp(1.0 - math.log10(rank) / CONSENSUS_RANK_LOG_DIVISOR)
        return f, f"EDHREC popularity rank #{rank:,} across the format"
    return None, "format consensus unavailable (no EDHREC data cached)"


def _f_synergy(name: str, ctx: DeckContext,
               synergy_pct: Optional[float],
               lift_score: Optional[float]) -> tuple[Optional[float], str]:
    """Does it fit THIS deck? EDHREC synergy blended with corpus lift.

    When the harvested corpus is under ``MIN_CORPUS_DECKS`` (10) — or
    the card simply has no lift row — the blend RENORMALIZES to the
    EDHREC term alone. It never feeds a zero: "we have not harvested
    enough decks to measure this" and "this card has no fit" are
    opposite conclusions.
    """
    if lift_score is None:
        lift_score = ctx.lift_scores.get(_key(name))
    corpus_ok = ctx.corpus_decks >= MIN_CORPUS_DECKS
    # ``is not None``, NOT truthiness: EDHREC synergy is a signed delta
    # from the format baseline, so 0.0 is a REAL measurement ("exactly
    # baseline here") and must stay a scored component. The old truthy
    # test renormalized a measured 0.0 away — which let a 0%-synergy
    # card OUTRANK a 0.1%-synergy card on this very component (its
    # weight shifted onto stronger components) while a -0.1% clamped to
    # a scored hard zero. Callers with no figure must pass None, never
    # 0.0. (Negative synergy still clamps to 0.0 — measured bad.)
    edh = (_clamp(synergy_pct / SYNERGY_SATURATION_PCT)
           if synergy_pct is not None else None)
    lift = (_clamp((lift_score - 1.0) / SYNERGY_LIFT_SATURATION)
            if (corpus_ok and lift_score is not None) else None)

    if edh is not None and lift is not None:
        f = SYNERGY_EDHREC_SHARE * edh + SYNERGY_LIFT_SHARE * lift
        return f, (f"EDHREC synergy {synergy_pct:.0f}% and lift "
                   f"{lift_score:.2f} against your harvested corpus "
                   f"({ctx.corpus_decks} decks)")
    if edh is not None:
        why = (f"corpus under {MIN_CORPUS_DECKS} decks"
               if not corpus_ok else "no corpus lift row for this card")
        return edh, (f"EDHREC synergy {synergy_pct:.0f}% for this "
                     f"commander — corpus lift not blended in ({why})")
    if lift is not None:
        return lift, (f"lift {lift_score:.2f} against your harvested "
                      f"corpus — no EDHREC synergy figure available")
    return None, "deck fit unavailable (no EDHREC synergy, no corpus lift)"


def _role_target_for(
    role: str, ctx: DeckContext,
) -> Optional[tuple[int, int, int]]:
    """``(count, target, saturation)`` for ``role``, or None when the
    role has no configured target AND no saturation threshold.

    ``win_condition`` folds into ``finisher``: ``role_target_report``
    already sums those two counts under one target, and the extended
    taxonomy is what tags Craterhoof / Coalition Victory.
    """
    if role == "win_condition":
        role = "finisher"
    report = ctx.role_report or {}
    entry = (report.get("roles") or {}).get(role)
    sat = ROLE_SATURATION_THRESHOLDS.get(role)
    if entry is not None:
        return int(entry["count"]), int(entry["target"]), int(
            sat if sat is not None else entry["target"]
        )
    if sat is None:
        return None
    # Role with a saturation ceiling but no build target (``tutor``):
    # target 0 means "the deck does not need any", so the score decays
    # from the first copy toward the ceiling instead of rewarding a
    # deficit that does not exist.
    try:
        counts = ctx.deck_role_counts  # memoized; ctx-injected lookup only
    except Exception:  # noqa: BLE001
        return None
    return int(counts.get(role, 0)), 0, int(sat)


def _f_role_fit(name: str, ctx: DeckContext,
                role: Optional[str]) -> tuple[Optional[float], str]:
    """Does the deck need this job done?

    The deficit-driven term — the thing that makes this deck-relative
    rather than a global power ranking. A role the deck is short on
    scores above 0.5 and rises with the size of the deficit; a role at
    target scores 0.5 and decays to 0 as it approaches
    ``ROLE_SATURATION_THRESHOLDS``.
    """
    role = role or ctx.role_of(name)
    if not role or role == "unknown":
        return None, "role unavailable (card text not resolvable)"
    numbers = _role_target_for(role, ctx)
    if numbers is None:
        return None, (f"role '{role}' has no build target — deck need "
                      f"not scored")
    count, target, sat = numbers
    label = "finisher" if role == "win_condition" else role
    if target > 0 and count < target:
        f = 0.5 + 0.5 * (target - count) / target
        return f, (f"deck is short on {label}: {count} of {target} "
                   f"(this card is a {label})")
    if sat > target and count < sat:
        f = 0.5 * (1.0 - (count - target) / (sat - target))
        return f, (f"deck already has {count} {label} against a target of "
                   f"{target} (saturates at {sat})")
    return 0.0, (f"deck is saturated on {label}: {count} already, "
                 f"ceiling {sat}")


@lru_cache(maxsize=64)
def _archetype_curve_cached(archetype_key: str,
                            bracket_key: Optional[int]) -> tuple[
                                tuple[int, float], ...]:
    """Memoized core of :func:`archetype_curve` — a pure function of its
    two (normalized) inputs, but called once per scored card via
    ``_f_curve_fit``. Returns an immutable tuple so cached state can't be
    mutated through a caller's dict."""
    tilt = ARCHETYPE_CURVE_TILT.get(archetype_key, 0.0)
    if bracket_key is not None:
        tilt += BRACKET_CURVE_TILT.get(bracket_key, 0.0)
    raw: dict[int, float] = {}
    for mv, base in TARGET_CURVE_BASE.items():
        factor = 1.0 - tilt * (CURVE_TILT_PIVOT - mv) / CURVE_TILT_PIVOT
        raw[mv] = base * max(0.1, factor)
    total = sum(raw.values()) or 1.0
    return tuple(
        (mv, v * CURVE_NONLAND_SLOTS / total) for mv, v in raw.items()
    )


def archetype_curve(archetype: Optional[str] = None,
                    bracket: Optional[int] = None) -> dict[int, float]:
    """Target NONLAND mana-value histogram for an archetype + bracket.

    Tilts :data:`TARGET_CURVE_BASE` around :data:`CURVE_TILT_PIVOT` and
    renormalizes back to :data:`CURVE_NONLAND_SLOTS`, so a tilt changes
    the SHAPE of the curve without changing how many spells it asks for.
    Negative tilt = shift down (aggro/combo/high bracket); positive =
    shift up (control/low bracket).
    """
    key = (archetype or "").lower()
    bracket_key = int(bracket) if bracket is not None else None
    return dict(_archetype_curve_cached(key, bracket_key))


def _f_curve_fit(name: str, ctx: DeckContext) -> tuple[Optional[float], str]:
    """Does it fit the curve the archetype wants?

    Lands are UNAVAILABLE here rather than 0.0: the target curve is a
    nonland curve, and scoring a land in its bucket-0 slot would measure
    something the model does not describe. Same for a card whose mana
    value we can't resolve.
    """
    card = ctx.card(name)
    if _is_land_card(name, card):
        return None, "curve fit not applicable to a land"
    mv = _cmc(card)
    if mv is None:
        return None, "curve fit unavailable (mana value not resolvable)"
    bucket = int(min(max(mv, 0.0), 7.0))
    target = archetype_curve(ctx.archetype, ctx.bracket)
    actual = ctx.curve
    want = target.get(bucket, 0.0)
    have = float(actual.get(bucket, 0))
    deficit = max(0.0, want - have)
    f = _clamp(deficit / max(1.0, want))
    label = f"{bucket}+" if bucket >= 7 else str(bucket)
    return f, (f"deck runs {have:.0f} spells at mana value {label}; this "
               f"curve wants about {want:.0f}")


def _f_mana_fit(name: str, ctx: DeckContext) -> tuple[Optional[float], str]:
    """Does it help meet Frank Karsten's color-source targets?

    Reads ``produced_mana`` — a field cached in every Scryfall snapshot
    and, before FP-015, read by exactly one land-only code path. A card
    that produces the deck's most under-served color scores near 1.0; a
    fifth Island when white is the problem scores 0.
    """
    report = ctx.manabase
    if not report:
        return None, "mana fit unavailable (manabase report unreadable)"
    identity = set(report.get("colors") or [])
    if not identity:
        return None, "mana fit not applicable (colorless deck)"
    card = ctx.card(name)
    produced = {p.upper() for p in ((card or {}).get("produced_mana") or [])
                if isinstance(p, str)}
    relevant = sorted(produced & identity)
    if not relevant:
        return 0.0, "produces no mana in the deck's colors"
    per_color = report.get("per_color") or {}
    parts: list[float] = []
    worst: list[str] = []
    for c in relevant:
        entry = per_color.get(c) or {}
        target = int(entry.get("target", 0) or 0)
        deficit = int(entry.get("deficit", 0) or 0)
        parts.append(_clamp(deficit / max(1, target)))
        if deficit > 0:
            worst.append(f"{c} is {deficit} source(s) short of {target}")
    f = sum(parts) / len(parts)
    detail = "; ".join(worst) if worst else (
        f"{'/'.join(relevant)} already at target"
    )
    return f, f"produces {'/'.join(relevant)} — {detail}"


# ---------------------------------------------------------------------------
# Modifiers
# ---------------------------------------------------------------------------

def _mod_combo(name: str, ctx: DeckContext) -> Optional[Modifier]:
    """``combo_completion`` (+15) / ``combo_partial`` (+6).

    Completion routes through ``combo_detection.one_piece_away``, whose
    rows are already machine-readable exactly so a scorer can consume
    them without re-deriving anything. Partial is the 3-card case where
    adding this card gets the deck to 2 of 3.
    """
    key = _key(name)
    for row in ctx.one_piece_away:
        if _key(row.get("missing", "")) == key:
            have = ", ".join(row.get("have") or [])
            return Modifier(
                "combo_completion",
                CARD_SCORE_MODIFIERS["combo_completion"],
                f"completes a known combo with {have} "
                f"({row.get('produces', 'combo')})",
            )
    for combo in ctx.combo_pool:
        cards = combo.get("cards") or []
        if len(cards) != 3 or key not in {_key(c) for c in cards}:
            continue
        present = [c for c in cards
                   if _key(c) != key and _key(c) in ctx.deck_keys]
        if len(present) == 1:
            return Modifier(
                "combo_partial",
                CARD_SCORE_MODIFIERS["combo_partial"],
                f"puts the deck 2 pieces into a 3-card line with "
                f"{present[0]} ({combo.get('produces', 'combo')})",
            )
    return None


def _mod_redundancy(name: str, ctx: DeckContext) -> Optional[Modifier]:
    """+5 when the deck has fewer than 3 of an effect this card covers.

    Uses ``interaction.INTERACTION_CATEGORIES`` as the effect taxonomy —
    the coverage matrix already counts every category per deck.
    """
    report = ctx.interaction
    if not report:
        return None
    card = ctx.card(name)
    if not card:
        return None
    from .interaction import oracle_categories
    cats = oracle_categories(_oracle_text(card), _type_line(card))
    if not cats:
        return None
    categories = report.get("categories") or {}
    thin = []
    for cat in sorted(cats):
        entry = categories.get(cat)
        if entry is None:
            continue
        count = int(entry.get("count", 0) or 0)
        if _key(name) in ctx.deck_keys:
            count -= 1  # don't credit a card for its own presence
        if count < REDUNDANCY_THRESHOLD:
            thin.append((cat, count))
    if not thin:
        return None
    cat, count = thin[0]
    return Modifier(
        "redundancy_relief", CARD_SCORE_MODIFIERS["redundancy_relief"],
        f"deck runs only {count} card(s) covering "
        f"{cat.replace('_', ' ')}; this adds another",
    )


def _mod_owned(name: str, ctx: DeckContext) -> Optional[Modifier]:
    if not ctx.collection_bias or not ctx.collection_keys:
        return None
    from .collection import owns
    try:
        if not owns(ctx.collection_keys, name):
            return None
    except Exception:  # noqa: BLE001
        return None
    return Modifier("owned", CARD_SCORE_MODIFIERS["owned"],
                    "you already own a copy")


def _mod_price(name: str, ctx: DeckContext) -> Optional[Modifier]:
    cap = ctx.price_soft_cap
    if cap is None:
        return None
    card = ctx.card(name)
    prices = (card or {}).get("prices") or {}
    raw = prices.get("usd")
    try:
        usd = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        usd = None
    if usd is None or usd <= cap:
        return None
    points = CARD_SCORE_MODIFIERS["price_penalty"] * _clamp(
        (usd - cap) / cap,
    )
    return Modifier("price_penalty", points,
                    f"${usd:,.2f} is over your ${cap:,.2f} soft cap")


def _mod_salt(name: str, ctx: DeckContext) -> Optional[Modifier]:
    """Salt only reads as a bracket mismatch at B <= 3.

    At B4/B5 the table has opted into this kind of card, so charging for
    it there would be the scorer imposing a preference the user already
    declined.
    """
    if ctx.bracket is None or int(ctx.bracket) > SALT_MAX_BRACKET:
        return None
    salt_map = ctx.salt_scores
    if not salt_map:
        return None
    salt = salt_map.get(_key(name))
    if salt is None or salt <= SALT_THRESHOLD:
        return None
    points = CARD_SCORE_MODIFIERS["salt_penalty"] * _clamp(
        (salt - SALT_THRESHOLD) / SALT_SATURATION_BAND,
    )
    return Modifier(
        "salt_penalty", points,
        f"EDHREC salt {salt:.1f} — plays poorly at bracket {ctx.bracket}",
    )


def _mod_bracket_pressure(name: str, ctx: DeckContext) -> Optional[Modifier]:
    """Tutor density, fast mana, extra turns and MLD against the bracket.

    Each trigger is a WotC bracket rule, not a taste judgement, and the
    total is clamped to -20 so a single card can never be charged more
    than the modifier's documented floor.
    """
    if ctx.bracket is None:
        return None
    from .bracket_estimator import (
        _EXTRA_TURN_CARDS, _FAST_MANA_CARDS, _MLD_CARDS, _TUTOR_CARDS,
    )
    bracket = int(ctx.bracket)
    key = _key(name)
    in_deck = key in ctx.deck_keys
    points = 0.0
    reasons: list[str] = []

    if key in _MLD_CARDS and bracket <= 3:
        points += _PRESSURE_MLD
        reasons.append(
            f"mass land denial is prohibited at bracket {bracket}",
        )
    if key in _EXTRA_TURN_CARDS:
        already = ctx.extra_turn_count - (1 if in_deck else 0)
        if bracket <= 2:
            points += _PRESSURE_EXTRA_TURN
            reasons.append(f"extra-turn spells are not allowed at "
                           f"bracket {bracket}")
        elif bracket == 3 and already >= 1:
            points += _PRESSURE_EXTRA_TURN
            reasons.append(f"deck already runs {already} extra-turn "
                           f"spell(s); bracket 3 forbids chaining them")
    budget = TUTOR_BUDGET.get(bracket)
    if budget is not None and key in _TUTOR_CARDS:
        already = ctx.tutor_count - (1 if in_deck else 0)
        if already >= budget:
            points += _PRESSURE_TUTOR
            reasons.append(f"deck already runs {already} tutor(s); "
                           f"bracket {bracket} wants at most {budget}")
    fm_budget = FAST_MANA_BUDGET.get(bracket)
    if fm_budget is not None and key in _FAST_MANA_CARDS:
        already = ctx.fast_mana_count - (1 if in_deck else 0)
        if already >= fm_budget:
            points += _PRESSURE_FAST_MANA
            reasons.append(f"deck already runs {already} fast-mana "
                           f"piece(s); bracket {bracket} wants at most "
                           f"{fm_budget}")
    if not reasons:
        return None
    floor = CARD_SCORE_MODIFIERS["bracket_pressure"]
    return Modifier("bracket_pressure", max(floor, points),
                    "; ".join(reasons))


def _mod_mdfc(name: str, ctx: DeckContext) -> Optional[Modifier]:
    if not ctx.is_mdfc_land(name):
        return None
    return Modifier("mdfc_bonus", CARD_SCORE_MODIFIERS["mdfc_bonus"],
                    "modal double-faced land — counts as half a land "
                    "without costing a spell slot")


_MODIFIER_FNS = (
    _mod_combo, _mod_redundancy, _mod_owned, _mod_price, _mod_salt,
    _mod_bracket_pressure, _mod_mdfc,
)


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------

def score_card(
    card_name: str,
    ctx: DeckContext,
    *,
    inclusion_pct: Optional[float] = None,
    synergy_pct: Optional[float] = None,
    lift_score: Optional[float] = None,
    role: Optional[str] = None,
    apply_gates: bool = True,
) -> CardScore:
    """``Gate(card) * [100 * Σ w_k f_k + Σ m_j]``, clamped to ``[0, 100]``.

    ``inclusion_pct`` / ``synergy_pct`` / ``lift_score`` / ``role`` are
    OPTIONAL pre-computed context: the advisor already holds all four at
    its ranking seam and passing them avoids a redundant lookup. Omit
    any of them and this derives what it can (``edhrec_rank`` for
    consensus, ``ctx.lift_scores`` for lift, ``classify_role_extended``
    for role) and renormalizes past what it can't.

    ``apply_gates=False`` computes the base+modifiers without zeroing —
    used by cut scoring, where "already in the deck" is the premise, not
    a singleton violation.

    Never raises: a bad card, a dead Scryfall or a weird deck degrades
    to a renormalized partial score, because a ranking loop that throws
    on one of two hundred candidates is worse than a coarse ordering.
    """
    gates = _gates(card_name, ctx) if apply_gates else ()

    raw: dict[str, tuple[Optional[float], str]] = {
        "consensus": _f_consensus(card_name, ctx, inclusion_pct),
        "synergy": _f_synergy(card_name, ctx, synergy_pct, lift_score),
        "role_fit": _f_role_fit(card_name, ctx, role),
        "curve_fit": _f_curve_fit(card_name, ctx),
        "mana_fit": _f_mana_fit(card_name, ctx),
    }
    # RENORMALIZE, never zero-fill: unavailable != bad.
    available_weight = sum(
        CARD_SCORE_WEIGHTS[k] for k, (v, _) in raw.items() if v is not None
    )
    components: dict[str, Component] = {}
    base = 0.0
    for name_k, (value, why) in raw.items():
        weight = CARD_SCORE_WEIGHTS[name_k]
        eff = (weight / available_weight
               if (value is not None and available_weight > 0) else 0.0)
        comp = Component(name_k, value, weight, why, eff)
        components[name_k] = comp
        base += comp.points

    modifiers: list[Modifier] = []
    for fn in _MODIFIER_FNS:
        try:
            mod = fn(card_name, ctx)
        except Exception:  # noqa: BLE001 — ranking must never raise
            mod = None
        if mod is not None:
            modifiers.append(mod)

    total = _clamp(base + sum(m.points for m in modifiers), 0.0, 100.0)
    if any(not g.passed for g in gates):
        total = 0.0
    return CardScore(card=card_name, total=total, base=base,
                     components=components, modifiers=tuple(modifiers),
                     gates=gates)


# ---------------------------------------------------------------------------
# Cut scoring
# ---------------------------------------------------------------------------

def _cut_block_reason(card_name: str, ctx: DeckContext) -> str:
    """Which guard rail (if any) forbids cutting ``card_name``.

    FP-015's contract is that switching cuts from alphabetical to scored
    must not lose a single rail that already existed in the codebase — so
    every rail below except one is a restatement of an existing refusal.

    The exception is the POLITICS rail, added 2026-08-17 by decision C2:
    it is not a deck-arithmetic rule at all but a statement about the
    measuring instrument, and it is shared with the advisor's own cut
    paths through ``staples.politics_tags``.

    Reported in priority order — the first rail that fires owns the
    message, so an explicit user ``Protect=`` is always the reason a user
    is given for their own lock.
    """
    key = _key(card_name)
    if key in ctx.protected_keys:
        return f"{card_name} is a Protect= line — the user locked it"
    # Politics rail (decision C2, 2026-08-17). The one rail here that is
    # NOT about the deck's own arithmetic: it is about the MEASURING
    # INSTRUMENT. Every cut this module ranks is ultimately validated by
    # an A/B margin from Forge's AI, which cannot goad, race the monarch,
    # bargain in a vote or notice a Rhystic tax — so for these cards a
    # flat margin says nothing about the card and everything about the
    # sim. Refuse the cut rather than down-rank it, same as every rail
    # above and below. ``PoliticsGuard=off`` in the deck's ``[metadata]``
    # turns it off for that one deck.
    if ctx.politics_guard:
        tags = ctx.politics_tags_of(card_name)
        if tags:
            return (f"{card_name} is a politics card ({'/'.join(tags)}) — "
                    f"{POLITICS_SHIELD_REASON}")
    if key in ctx.combo_pieces:
        return (f"{card_name} is a piece of a combo the deck already "
                f"assembles")
    if _is_land_card(card_name, ctx.card(card_name)):
        remaining = ctx.effective_lands - 1.0
        if remaining < EFFECTIVE_LAND_FLOOR:
            return (f"cutting {card_name} drops the deck to "
                    f"{remaining:.1f} effective lands, below the "
                    f"{EFFECTIVE_LAND_FLOOR:.0f} floor")
    elif ctx.is_mdfc_land(card_name):
        remaining = ctx.effective_lands - 0.5
        if remaining < EFFECTIVE_LAND_FLOOR:
            return (f"cutting {card_name} drops the deck to "
                    f"{remaining:.1f} effective lands, below the "
                    f"{EFFECTIVE_LAND_FLOOR:.0f} floor")
    role = ctx.role_of(card_name)
    if role == "win_condition":
        role = "finisher"
    report = ctx.role_report or {}
    entry = (report.get("roles") or {}).get(role)
    if entry is not None:
        if int(entry["count"]) - 1 < int(entry["target"]):
            return (f"cutting {card_name} drops {role} to "
                    f"{int(entry['count']) - 1}, below its target of "
                    f"{int(entry['target'])}")
    return ""


def cut_score(card_name: str, ctx: DeckContext) -> CutScore:
    """``100 - CardScore(card | deck_without_card)``, plus guard rails.

    Recomputed against the deck MINUS that card — otherwise a saturated
    role charges every one of its members for its own presence and the
    weakest member never surfaces.
    """
    reason = _cut_block_reason(card_name, ctx)
    sub = ctx.without(card_name)
    inner = score_card(card_name, sub, apply_gates=False)
    return CutScore(card=card_name, score=_clamp(100.0 - inner.total,
                                                 0.0, 100.0),
                    card_score=inner, blocked=bool(reason),
                    block_reason=reason)


def like_for_like(add_role: Optional[str], cut_role: Optional[str]) -> bool:
    """Same-role trade check.

    Mirrors the constraint ``deck_builder_personalize.lift_swaps``
    enforces at lines 217-224: a swap with no same-role slot is skipped
    rather than allowed to distort the deck's role counts. Unknown roles
    on either side are not a match — an unclassified card is not a
    licence to trade across roles.
    """
    if not add_role or not cut_role:
        return False
    if add_role == "unknown" or cut_role == "unknown":
        return False
    a = "finisher" if add_role == "win_condition" else add_role
    b = "finisher" if cut_role == "win_condition" else cut_role
    return a == b


def cut_candidates(
    ctx: DeckContext,
    *,
    candidates: Optional[Iterable[str]] = None,
    for_role: Optional[str] = None,
) -> list[CutScore]:
    """Guard-railed cut candidates, worst-fitting first.

    Blocked cards are dropped, not merely down-ranked: a guard rail is a
    refusal, not a preference. Ordering is ``(-cut_score, name)`` so two
    identical runs over the same deck always propose the same cuts (the
    determinism the alphabetical loop bought with a comment conceding
    "no per-card score exists at this point").

    ``for_role`` applies the like-for-like constraint: pass the role of
    the card you intend to ADD and only same-role cuts come back.
    """
    names = (list(candidates) if candidates is not None
             else list(ctx.deck_cards))
    out: list[CutScore] = []
    for name in names:
        if for_role is not None and not like_for_like(for_role,
                                                      ctx.role_of(name)):
            continue
        scored = cut_score(name, ctx)
        if scored.blocked:
            continue
        out.append(scored)
    out.sort(key=lambda c: (-c.score, c.card))
    return out


def cut_order(deck_cards: Iterable[str], ctx: DeckContext,
              *, for_role: Optional[str] = None) -> list[str]:
    """Just the names from :func:`cut_candidates`, ready to iterate.

    The drop-in replacement for ``sorted(deck_cards)`` at
    ``_advisor_heuristic.py``'s cut loop.
    """
    return [c.card for c in cut_candidates(ctx, candidates=deck_cards,
                                           for_role=for_role)]
