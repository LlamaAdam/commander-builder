"""FP-015 addendum — reference corpus, whole-deck score, and bubble cards.

WHY THIS EXISTS (operator direction, 2026-07-25)
================================================
The advisor's historical contract — "propose 1–10 swaps" — forces churn
into decks that don't need it. FP-002's empirical result backs this up:
curation is ~neutral on average and adds the *least* to already-coherent
decks. What the operator actually wants is:

1. **A whole-deck verdict first** — is this deck already good? If yes,
   recommend few or zero changes instead of manufacturing ten.
2. **Bubble cards** — the cards that are *on the bubble*: weak in this
   deck (high ``cut_score``), rarely played in successful builds of the
   same commander (low reference support), and cheaply replaceable by a
   clearly better candidate. Those are the easy, low-regret swaps.
3. **Ground both in what good decks look like** — the top-liked
   Moxfield builds for the commander, EDHREC's average deck, and
   EDHREC's salt list. (A third reference site can plug into the
   injectable ``fetch_decks`` seam later — Archidekt is the obvious
   candidate; nothing here hard-codes Moxfield's shape beyond the
   extractor it borrows.)

Same honesty contract as ``card_score``: every number here is a
heuristic prior, not a verdict — Forge A/B sims remain the arbiter.
The deck score decides how much change to *propose*; it never claims to
predict win rate (FP-002 showed no pre-sim feature does).

Unavailable != bad: any component whose input is missing (no corpus, no
salt data, colorless manabase) is dropped and the remaining weights are
renormalized — mirroring the ``deck_health`` / ``card_score`` contract.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .card_score import DeckContext, cut_score, deck_context, score_card

# ---------------------------------------------------------------------------
# Tuning surface (hand-set priors, same pattern as CARD_SCORE_WEIGHTS)
# ---------------------------------------------------------------------------

#: Below this many reference decks, per-card support is statistically
#: meaningless and reads as None (the corpus still carries salt/average
#: data). Mirrors lift_analysis' MIN_CORPUS_DECKS contract.
MIN_REFERENCE_DECKS = 10

#: How many top-liked Moxfield decks to pull for the corpus.
DEFAULT_REFERENCE_N = 50

#: Cache lifetime for a fetched corpus. Liked-deck rankings move slowly.
CACHE_TTL_HOURS = 168

#: A card is support-bubble-eligible when at most this fraction of
#: reference decks run it.
BUBBLE_SUPPORT_CEILING = 0.25

#: ...and cut-bubble-eligible when its cut_score is at least this
#: (cut_score = 100 - CardScore(card | deck without it); high = weak).
BUBBLE_CUT_FLOOR = 55.0

#: A replacement must out-score the incumbent's in-deck score by at
#: least this margin to make the swap "easy" rather than a coin flip.
REPLACEMENT_MARGIN = 10.0

#: Reference-corpus cards at or above this support are replacement pool.
REPLACEMENT_SUPPORT_FLOOR = 0.4

#: Deck-level component weights (renormalized over available components).
DECK_SCORE_WEIGHTS: dict[str, float] = {
    "reference_alignment": 0.40,  # does the 99 look like successful builds
    "role_fit": 0.25,             # are the role targets met
    "mana_fit": 0.25,             # Karsten source targets met
    "salt_fit": 0.10,             # bracket-inappropriate salt (B<=3 only)
}

#: Mean nonland support at which reference_alignment saturates to 1.0.
#: Singleton variance means even archetypal decks average well under
#: full support; ~0.45 marks "this is what the community builds".
_ALIGNMENT_SATURATION = 0.45

#: Deck-level salt threshold (stricter than the per-card 1.5 warn line:
#: at deck level we only dock genuinely table-hostile picks).
_SALT_DECK_THRESHOLD = 2.0
#: Each salty card above threshold costs this much of the salt_fit
#: component (floor 0) at bracket <= 3.
_SALT_STEP = 0.2

#: Verdict bands over the 0-100 total.
VERDICT_KEEP = 75.0     # >= : good deck — 0-2 changes at most
VERDICT_POLISH = 55.0   # >= : solid — a few bubble swaps
# below VERDICT_POLISH: structural work first (FP-002: fix structure,
# then curate; swaps alone won't rescue a structurally deficient deck).


def _key(name: str) -> str:
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Reference corpus
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceCorpus:
    """What good decks for this commander look like.

    ``deck_card_keys`` is one frozenset of lowercase card names per
    reference deck (top-liked Moxfield builds). ``display_names`` maps
    key -> canonical printed name so replacement suggestions render
    properly. ``average_deck_keys`` is EDHREC's aggregate list;
    ``salt_map`` is EDHREC's key -> 0-5 salt score.
    """

    commander: str
    bracket: Optional[int]
    deck_card_keys: tuple[frozenset[str], ...]
    display_names: dict[str, str] = field(default_factory=dict)
    average_deck_keys: frozenset[str] = frozenset()
    salt_map: dict[str, float] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def n_decks(self) -> int:
        return len(self.deck_card_keys)

    def support(self, card_name: str) -> Optional[float]:
        """Fraction of reference decks running ``card_name``.

        None (not 0.0) below MIN_REFERENCE_DECKS — "we don't know" must
        never render as "nobody plays it".
        """
        if self.n_decks < MIN_REFERENCE_DECKS:
            return None
        k = _key(card_name)
        return sum(1 for d in self.deck_card_keys if k in d) / self.n_decks

    def in_average_deck(self, card_name: str) -> bool:
        return _key(card_name) in self.average_deck_keys

    def replacement_pool(
        self,
        exclude_keys: frozenset[str],
        min_support: float = REPLACEMENT_SUPPORT_FLOOR,
    ) -> list[str]:
        """High-consensus cards NOT already in the deck, support desc.

        Cards the successful builds agree on that this deck skips —
        the natural candidates to replace bubble cards with. Falls back
        to the EDHREC average deck when the Moxfield corpus is thin.
        """
        scored: list[tuple[float, str]] = []
        if self.n_decks >= MIN_REFERENCE_DECKS:
            counts: dict[str, int] = {}
            for deck in self.deck_card_keys:
                for k in deck:
                    counts[k] = counts.get(k, 0) + 1
            for k, c in counts.items():
                s = c / self.n_decks
                if s >= min_support and k not in exclude_keys:
                    scored.append((s, k))
        else:
            for k in self.average_deck_keys:
                if k not in exclude_keys:
                    scored.append((0.5, k))
        scored.sort(reverse=True)
        return [self.display_names.get(k, k) for _s, k in scored]

    def to_dict(self) -> dict:
        return {
            "commander": self.commander,
            "bracket": self.bracket,
            "decks": [sorted(d) for d in self.deck_card_keys],
            "display_names": self.display_names,
            "average_deck": sorted(self.average_deck_keys),
            "salt_map": self.salt_map,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReferenceCorpus":
        return cls(
            commander=payload.get("commander", ""),
            bracket=payload.get("bracket"),
            deck_card_keys=tuple(
                frozenset(d) for d in payload.get("decks", [])
            ),
            display_names=dict(payload.get("display_names", {})),
            average_deck_keys=frozenset(payload.get("average_deck", [])),
            salt_map={k: float(v)
                      for k, v in (payload.get("salt_map") or {}).items()},
            fetched_at=float(payload.get("fetched_at", 0.0)),
        )


def _ref_cache_dir() -> Path:
    # Same tree as the EDHREC caches so everything lives under mtg_cards.
    from .edhrec_client import CACHE_DIR
    return CACHE_DIR.parent / "ref_corpus"


def _cache_path(commander: str, bracket: Optional[int]) -> Path:
    from .edhrec_client import commander_slug
    suffix = f"_b{bracket}" if bracket else ""
    return _ref_cache_dir() / f"{commander_slug(commander)}{suffix}.json"


def _default_fetch_decks(commander: str, bracket: Optional[int],
                         n: int) -> list[dict]:
    from .moxfield_import import find_top_liked_decks_for_commander
    return find_top_liked_decks_for_commander(commander, bracket=bracket, n=n)


def _default_fetch_average(commander: str) -> list[str]:
    from .edhrec_client import fetch_average_deck
    try:
        avg = fetch_average_deck(commander)
    except Exception:  # noqa: BLE001 — degrade, don't die, on network loss
        return []
    # fetch_average_deck returns an AverageDeck whose .cards is a list of
    # CardEntry (mainboard + commander mixed) — caught live 2026-07-25;
    # the injected-fetcher tests couldn't see the real shape.
    entries = getattr(avg, "cards", None) or []
    out: list[str] = []
    for entry in entries:
        name = getattr(entry, "name", None) or (
            entry.get("name") if isinstance(entry, dict) else None)
        if name:
            out.append(name)
    return out


def _default_fetch_salt() -> dict[str, float]:
    from .edhrec_client import fetch_salt_list
    try:
        return fetch_salt_list() or {}
    except Exception:  # noqa: BLE001
        return {}


def _default_fetch_extra_lists(commander: str, bracket: Optional[int],
                               n: int) -> list[list[str]]:
    """Archidekt — the corpus' third source (plain card-name lists)."""
    from .archidekt_client import fetch_top_decks
    try:
        return fetch_top_decks(commander, bracket=bracket, n=n)
    except Exception:  # noqa: BLE001 — a dead source shrinks the corpus,
        return []      # it never sinks the build


def build_reference_corpus(
    commander: str,
    bracket: Optional[int] = None,
    n: int = DEFAULT_REFERENCE_N,
    *,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
    fetch_decks: Optional[Callable[[str, Optional[int], int],
                                   list[dict]]] = None,
    fetch_average: Optional[Callable[[str], list[str]]] = None,
    fetch_salt: Optional[Callable[[], dict[str, float]]] = None,
    fetch_extra_lists: Optional[Callable[[str, Optional[int], int],
                                         list[list[str]]]] = None,
) -> Optional[ReferenceCorpus]:
    """Fetch (or load cached) reference data for ``commander``.

    ~``n`` Moxfield deck fetches on a cold cache — a deliberate,
    cacheable operation, not something to run per keystroke. Returns
    None only when EVERY source came back empty (no decks, no average
    deck): there is nothing to reference against. The salt list alone
    doesn't qualify — it isn't commander-specific.

    ``fetch_decks(commander, bracket, n)`` / ``fetch_average(commander)``
    / ``fetch_salt()`` / ``fetch_extra_lists(commander, bracket, n)``
    are injectable for tests. ``fetch_extra_lists`` defaults to the
    Archidekt top-viewed decks (plain card-name lists, capped at its own
    smaller request budget) and merges into the same reference pool —
    ``support()`` is denominatored over ALL merged decks.
    """
    path = _cache_path(commander, bracket)
    if cache and path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h <= ttl_hours:
            try:
                return ReferenceCorpus.from_dict(
                    json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — corrupt cache = refetch
                pass

    from ._advisor_bracket_peers import _extract_main_cards_from_moxfield_json

    deck_jsons = (fetch_decks or _default_fetch_decks)(commander, bracket, n)
    display: dict[str, str] = {}
    deck_sets: list[frozenset[str]] = []
    for dj in deck_jsons or []:
        names = _extract_main_cards_from_moxfield_json(dj)
        if not names:
            continue
        keys = set()
        for name in names:
            k = _key(name)
            keys.add(k)
            display.setdefault(k, name)
        deck_sets.append(frozenset(keys))

    from .archidekt_client import DEFAULT_N as _ARCHIDEKT_N
    extra_lists = (fetch_extra_lists or _default_fetch_extra_lists)(
        commander, bracket, min(n, _ARCHIDEKT_N),
    )
    for names in extra_lists or []:
        keys = set()
        for name in names:
            k = _key(name)
            keys.add(k)
            display.setdefault(k, name)
        if keys:
            deck_sets.append(frozenset(keys))

    avg_names = (fetch_average or _default_fetch_average)(commander)
    avg_keys = set()
    for name in avg_names or []:
        k = _key(name)
        avg_keys.add(k)
        display.setdefault(k, name)

    if not deck_sets and not avg_keys:
        return None

    corpus = ReferenceCorpus(
        commander=commander,
        bracket=bracket,
        deck_card_keys=tuple(deck_sets),
        display_names=display,
        average_deck_keys=frozenset(avg_keys),
        salt_map={_key(k): float(v)
                  for k, v in ((fetch_salt or _default_fetch_salt)() or {}
                               ).items()},
        fetched_at=time.time(),
    )
    if cache:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(corpus.to_dict()), encoding="utf-8")
        except Exception:  # noqa: BLE001 — cache write failure is not fatal
            pass
    return corpus


# ---------------------------------------------------------------------------
# Whole-deck score
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeckScoreReport:
    """0-100 whole-deck verdict + the change budget it implies."""

    total: float
    verdict: str                       # "keep" | "polish" | "overhaul"
    change_budget: tuple[int, int]     # (min, max) swaps worth proposing
    components: dict[str, dict]        # name -> {value, weight, detail}
    n_reference_decks: int
    explanations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 1),
            "verdict": self.verdict,
            "change_budget": list(self.change_budget),
            "components": self.components,
            "n_reference_decks": self.n_reference_decks,
            "explanations": list(self.explanations),
        }


def _component_reference_alignment(
    ctx: DeckContext, corpus: Optional[ReferenceCorpus],
) -> Optional[tuple[float, str]]:
    if corpus is None:
        return None
    nonlands = [c for c in ctx.deck_cards
                if not ctx.card(c) or "Land" not in
                (ctx.card(c) or {}).get("type_line", "")]
    supports = [s for s in (corpus.support(c) for c in nonlands)
                if s is not None]
    if not supports:
        return None
    mean = sum(supports) / len(supports)
    value = min(1.0, mean / _ALIGNMENT_SATURATION)
    return value, (f"mean reference support {mean:.2f} over "
                   f"{len(supports)} nonland cards "
                   f"({corpus.n_decks} reference decks)")


def _component_role_fit(ctx: DeckContext) -> Optional[tuple[float, str]]:
    report = ctx.role_report
    if not report:
        return None
    roles = report.get("roles") or {}
    total_target = sum(v.get("target", 0) for v in roles.values())
    if total_target <= 0:
        return None
    total_deficit = sum(v.get("deficit", 0) for v in roles.values())
    value = max(0.0, 1.0 - total_deficit / total_target)
    under = report.get("under_built") or []
    detail = ("all role targets met" if not under
              else "under-built: " + ", ".join(under))
    return value, detail


def _component_mana_fit(ctx: DeckContext) -> Optional[tuple[float, str]]:
    mb = ctx.manabase
    if not mb:
        return None
    total_target = mb.get("total_target") or 0
    if total_target <= 0:
        return None  # colorless / n-a, never "perfect"
    deficit = mb.get("total_deficit") or 0
    value = max(0.0, 1.0 - deficit / total_target)
    under = mb.get("under_served") or []
    detail = ("Karsten source targets met" if not under
              else "under-served colors: " + ", ".join(under))
    return value, detail


def _component_salt_fit(
    ctx: DeckContext, corpus: Optional[ReferenceCorpus],
) -> Optional[tuple[float, str]]:
    if ctx.bracket is None or ctx.bracket > 3:
        return None  # anything goes at B4/B5
    salt = ctx.salt_scores if ctx.salt_scores is not None else (
        corpus.salt_map if corpus else None)
    if salt is None:
        return None
    salty = [c for c in ctx.deck_cards
             if salt.get(_key(c), 0.0) >= _SALT_DECK_THRESHOLD]
    value = max(0.0, 1.0 - _SALT_STEP * len(salty))
    detail = ("no table-hostile cards for this bracket" if not salty
              else f"{len(salty)} high-salt card(s) at B{ctx.bracket}: "
                   + ", ".join(sorted(salty)[:5]))
    return value, detail


def score_deck(
    deck_text: str = "",
    corpus: Optional[ReferenceCorpus] = None,
    ctx: Optional[DeckContext] = None,
    **ctx_kwargs,
) -> DeckScoreReport:
    """Whole-deck 0-100 score -> verdict -> change budget.

    The budget replaces "always propose k swaps": a ``keep`` deck earns
    0-2 proposals, ``polish`` 2-5, and ``overhaul`` means structural
    work (roles/manabase) should come before card swaps at all —
    FP-002's fix-structure-first finding, operationalized.
    """
    if ctx is None:
        if corpus is not None and "salt_scores" not in ctx_kwargs:
            ctx_kwargs["salt_scores"] = corpus.salt_map
        ctx = deck_context(deck_text=deck_text, **ctx_kwargs)

    raw: dict[str, Optional[tuple[float, str]]] = {
        "reference_alignment": _component_reference_alignment(ctx, corpus),
        "role_fit": _component_role_fit(ctx),
        "mana_fit": _component_mana_fit(ctx),
        "salt_fit": _component_salt_fit(ctx, corpus),
    }
    available = {k: v for k, v in raw.items() if v is not None}
    components: dict[str, dict] = {}
    explanations: list[str] = []
    if available:
        weight_sum = sum(DECK_SCORE_WEIGHTS[k] for k in available)
        total = 0.0
        for name, (value, detail) in available.items():
            w = DECK_SCORE_WEIGHTS[name] / weight_sum
            total += w * value
            components[name] = {"value": round(value, 3),
                                "weight": round(w, 3),
                                "detail": detail}
            explanations.append(f"{name}: {detail}")
        total *= 100.0
    else:
        total = 50.0  # nothing measurable: pure agnosticism, mid-band
        explanations.append(
            "no component had usable inputs — score is uninformative")
    for name in raw:
        if name not in available:
            components[name] = {"value": None, "weight": 0.0,
                                "detail": "unavailable — skipped"}

    if not available:
        # Nothing measurable is agnosticism, not a structural indictment
        # — land in the middle band, never "overhaul".
        verdict, budget = "polish", (2, 5)
        explanations.append(
            "score is uninformative — treating as mid-band by default")
    elif total >= VERDICT_KEEP:
        verdict, budget = "keep", (0, 2)
        explanations.append(
            "deck is already close to what good builds look like — "
            "recommend at most cosmetic bubble swaps")
    elif total >= VERDICT_POLISH:
        verdict, budget = "polish", (2, 5)
        explanations.append("solid core — a few bubble swaps are worth it")
    else:
        verdict, budget = "overhaul", (0, 0)
        explanations.append(
            "structural deficits dominate — fix roles/manabase before "
            "swapping cards (FP-002: curation adds least to structurally "
            "deficient decks)")

    return DeckScoreReport(
        total=total,
        verdict=verdict,
        change_budget=budget,
        components=components,
        n_reference_decks=corpus.n_decks if corpus else 0,
        explanations=tuple(explanations),
    )


# ---------------------------------------------------------------------------
# Bubble cards
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BubbleCard:
    """A card that is easy to replace, with the best replacement found."""

    card: str
    cut_score: float
    support: Optional[float]
    salt: float
    replacement: Optional[dict]     # {card, score, support} or None
    ease: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "card": self.card,
            "cut_score": round(self.cut_score, 1),
            "support": (round(self.support, 3)
                        if self.support is not None else None),
            "salt": round(self.salt, 2),
            "replacement": self.replacement,
            "ease": round(self.ease, 1),
            "reasons": list(self.reasons),
        }


def find_bubble_cards(
    deck_text: str = "",
    corpus: Optional[ReferenceCorpus] = None,
    ctx: Optional[DeckContext] = None,
    candidates: Optional[Iterable[str]] = None,
    max_results: int = 10,
    scorer: Callable = score_card,
    cutter: Callable = cut_score,
    **ctx_kwargs,
) -> list[BubbleCard]:
    """Rank the deck's easy replacements, best-first.

    A card is *on the bubble* when it is weak in this deck
    (``cut_score >= BUBBLE_CUT_FLOOR``) AND the reference corpus
    doesn't vouch for it (support <= BUBBLE_SUPPORT_CEILING, or no
    corpus at all). Guard-railed (blocked) and protected cards never
    qualify — the cut guard rails already refuse role-breaking,
    combo-breaking, and land-floor cuts.

    Each bubble card is paired with the best replacement from
    ``candidates`` (or the corpus' high-support pool) that out-scores
    its in-deck score by ``REPLACEMENT_MARGIN``; replacements are
    consumed greedily so two bubble cards never claim the same add.
    ``ease`` orders the list: score gap x replacement consensus.

    ``scorer`` / ``cutter`` default to the real card_score functions and
    are injectable for tests.
    """
    if ctx is None:
        if corpus is not None and "salt_scores" not in ctx_kwargs:
            ctx_kwargs["salt_scores"] = corpus.salt_map
        ctx = deck_context(deck_text=deck_text, **ctx_kwargs)

    salt = (ctx.salt_scores if ctx.salt_scores is not None
            else (corpus.salt_map if corpus else {})) or {}

    bubbles: list[tuple[float, str, object, Optional[float], list[str]]] = []
    for card in ctx.deck_cards:
        if _key(card) in ctx.protected_keys:
            continue
        cut = cutter(card, ctx)
        if getattr(cut, "blocked", False):
            continue
        if cut.score < BUBBLE_CUT_FLOOR:
            continue
        support = corpus.support(card) if corpus else None
        reasons = [f"weak in this deck (cut score {cut.score:.0f})"]
        if support is not None:
            if support > BUBBLE_SUPPORT_CEILING:
                continue  # the community vouches for it — not a bubble
            reasons.append(
                f"only {support:.0%} of {corpus.n_decks} reference "
                f"decks run it")
        else:
            reasons.append("no reference corpus — cut-score only")
        card_salt = salt.get(_key(card), 0.0)
        if card_salt >= _SALT_DECK_THRESHOLD:
            reasons.append(f"salt {card_salt:.1f}")
        bubbles.append((cut.score, card, cut, support, reasons))

    # Replacement pool: caller-supplied candidates win; else the corpus'
    # high-consensus absentees. Score each once (score_card is
    # deck-relative, not slot-relative) and hand out greedily.
    if candidates is not None:
        pool = [c for c in candidates if _key(c) not in ctx.deck_keys]
    elif corpus is not None:
        pool = corpus.replacement_pool(ctx.deck_keys)
    else:
        pool = []
    pool_scored: list[tuple[float, str, Optional[float]]] = []
    for cand in pool:
        # Lands never replace nonland bubble cards — the manabase
        # pipeline owns land counts. (Caught live 2026-07-25: Temple
        # Garden outscored real spell candidates via its mana_fit
        # component and was offered as a creature's replacement.)
        cand_card = ctx.card(cand)
        if cand_card and "Land" in (cand_card.get("type_line") or ""):
            continue
        try:
            sc = scorer(cand, ctx)
        except Exception:  # noqa: BLE001 — one bad candidate, not the run
            continue
        total = getattr(sc, "total", None)
        if total is None:
            continue
        if total <= 0:  # gated out (illegal / off-color / duplicate)
            continue
        pool_scored.append(
            (float(total), cand,
             corpus.support(cand) if corpus else None))
    pool_scored.sort(reverse=True)

    out: list[BubbleCard] = []
    used: set[str] = set()
    for cut_val, card, _cut, support, reasons in sorted(bubbles,
                                                        reverse=True):
        in_deck_score = 100.0 - cut_val
        replacement = None
        for total, cand, cand_support in pool_scored:
            if _key(cand) in used:
                continue
            if total < in_deck_score + REPLACEMENT_MARGIN:
                break  # sorted desc — nothing further qualifies
            replacement = {"card": cand, "score": round(total, 1),
                           "support": (round(cand_support, 3)
                                       if cand_support is not None
                                       else None)}
            used.add(_key(cand))
            break
        gap = (replacement["score"] - in_deck_score) if replacement else 0.0
        consensus = 0.5
        if replacement and replacement["support"] is not None:
            consensus = 0.5 + 0.5 * replacement["support"]
        ease = gap * consensus
        out.append(BubbleCard(
            card=card,
            cut_score=cut_val,
            support=support,
            salt=salt.get(_key(card), 0.0),
            replacement=replacement,
            ease=ease,
            reasons=tuple(reasons),
        ))

    out.sort(key=lambda b: (b.ease, b.cut_score), reverse=True)
    return out[:max_results]


# ---------------------------------------------------------------------------
# Advisor integration
# ---------------------------------------------------------------------------

def apply_verdict_to_report(
    report,
    *,
    deck_text: str = "",
    corpus: Optional[ReferenceCorpus] = None,
    ctx: Optional[DeckContext] = None,
    score_fn: Optional[Callable] = None,
    bubble_fn: Optional[Callable] = None,
    **ctx_kwargs,
):
    """Attach the whole-deck verdict to an ``AdviceReport`` and trim its
    recommendations to the change budget. Returns a NEW report — the
    input is never mutated.

    This is the "stop forcing 1-10 swaps" seam:

    - ``verdict == "keep"`` / ``"polish"``: non-essential adds are
      capped at the budget (they arrive advisor-ranked, so the cap
      keeps the best); non-essential cuts are reordered **bubble-first**
      (easiest replacements lead, so the downstream i-mod-n add/cut
      pairing consumes bubble cards before coin-flip cuts) and capped
      at the same budget. Recs whose ``evidence.source`` ends with
      ``"_essentials"`` (manabase / tribal fixes) are structural and
      always survive the trim.
    - ``verdict == "overhaul"``: nothing is trimmed. The budget of 0
      swaps means "swaps alone won't fix this deck" — but the
      advisor's recommendations ARE the structural fixes, so they all
      stay. The verdict text tells the user to fix structure first.

    ``score_fn`` / ``bubble_fn`` default to :func:`score_deck` /
    :func:`find_bubble_cards` and are injectable for tests.
    """
    from dataclasses import replace

    if ctx is None:
        if corpus is not None and "salt_scores" not in ctx_kwargs:
            ctx_kwargs["salt_scores"] = corpus.salt_map
        ctx = deck_context(deck_text=deck_text, **ctx_kwargs)

    ds = (score_fn or score_deck)(corpus=corpus, ctx=ctx)
    bubbles = (bubble_fn or find_bubble_cards)(corpus=corpus, ctx=ctx)
    ds_dict = ds.to_dict() if hasattr(ds, "to_dict") else dict(ds)
    bubble_dicts = [b.to_dict() if hasattr(b, "to_dict") else dict(b)
                    for b in bubbles]

    verdict = ds_dict.get("verdict")
    budget_max = (ds_dict.get("change_budget") or [0, 0])[1]
    recs = list(report.recommendations)

    if verdict in ("keep", "polish"):
        def _essential(rec) -> bool:
            source = str((rec.evidence or {}).get("source", ""))
            return source.endswith("_essentials")

        bubble_rank = {str(b.get("card", "")).strip().lower(): i
                       for i, b in enumerate(bubble_dicts)}
        essentials = [r for r in recs if _essential(r)]
        adds = [r for r in recs
                if r.action == "add" and not _essential(r)]
        cuts = [r for r in recs
                if r.action == "cut" and not _essential(r)]
        other = [r for r in recs
                 if r.action not in ("add", "cut") and not _essential(r)]
        cuts.sort(key=lambda r: bubble_rank.get(
            r.card.strip().lower(), len(bubble_rank) + 1))
        recs = (essentials + other
                + adds[:budget_max] + cuts[:budget_max])

    return replace(report, recommendations=recs,
                   deck_score=ds_dict, bubble_cards=bubble_dicts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """``python -m commander_builder.bubble_analysis <deck.dck> [...]``"""
    parser = argparse.ArgumentParser(
        prog="commander-bubble",
        description="Whole-deck score + bubble-card (easy replacement) "
                    "report. Heuristic prior only — Forge sims remain "
                    "the arbiter.",
    )
    parser.add_argument("deck", help="path to a .dck file")
    parser.add_argument("--bracket", type=int, default=None)
    parser.add_argument("--refs", type=int, default=DEFAULT_REFERENCE_N,
                        help="reference decks to fetch (default %(default)s)")
    parser.add_argument("--no-network", action="store_true",
                        help="skip the reference corpus entirely")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    deck_path = Path(args.deck)
    if not deck_path.is_file():
        print(f"error: no such deck file: {deck_path}", file=sys.stderr)
        return 2
    deck_text = deck_path.read_text(encoding="utf-8", errors="replace")

    ctx = deck_context(deck_text=deck_text, bracket=args.bracket)
    corpus = None
    if not args.no_network and ctx.commander_names:
        corpus = build_reference_corpus(
            " // ".join(ctx.commander_names), bracket=args.bracket,
            n=args.refs)

    report = score_deck(corpus=corpus, ctx=ctx)
    bubbles = find_bubble_cards(corpus=corpus, ctx=ctx)

    if args.as_json:
        print(json.dumps({"deck_score": report.to_dict(),
                          "bubble_cards": [b.to_dict() for b in bubbles]},
                         indent=2))
        return 0

    print(f"Deck score: {report.total:.0f}/100 -> {report.verdict} "
          f"(propose {report.change_budget[0]}-{report.change_budget[1]} "
          f"changes)")
    for line in report.explanations:
        print(f"  - {line}")
    if bubbles:
        print(f"\nBubble cards ({len(bubbles)}):")
        for b in bubbles:
            repl = (f" -> {b.replacement['card']} "
                    f"(score {b.replacement['score']})"
                    if b.replacement else " (no clear replacement)")
            print(f"  {b.card}  [cut {b.cut_score:.0f}]{repl}")
            for r in b.reasons:
                print(f"      {r}")
    else:
        print("\nNo bubble cards — nothing is an easy replacement.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
