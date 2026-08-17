"""Adaptive change budget — deck-health score -> curation-intensity tier.

THE HONEST DIVISION OF LABOR (FP-002 / FP-015 closures, see
docs/future-plans.md): heuristic scores carry NO win-rate claim. The
deck-level health score (``deck_health.compute_health_grade``, 0-100)
is a *descriptive* construction measure — "this is a bad combination
of cards" — not a predictor of sim outcomes. So the score is allowed
to decide exactly one thing here: HOW MUCH to change (the budget
tier). WHAT actually stays in the deck is still decided by the
empirical Forge A/B verdict, the same as every other curation path.

Before this module, curation intensity was manually selected
(``--mode polish|overhaul|free``), so a 30/100 deck got the same timid
5-card polish as a 70/100 deck unless the operator remembered to
escalate. ``resolve_tier`` maps the score to a tier; callers opt in
via ``--mode auto`` (opt-in on purpose — budget escalation multiplies
Forge sim cost, and that spend is the operator's call, not a default).

TIER THRESHOLDS — named constants, with the reasoning:

- ``KEEP_MIN_SCORE = 75``: matches ``bubble_analysis.score_deck``'s
  ">= 75 keep" band — a deck this healthy earns minimal-change
  semantics (0-2 swaps), consistent with the existing keep behavior.
- ``POLISH_MIN_SCORE = 55``: matches the same module's ">= 55 polish"
  band; a solid-but-improvable deck gets the conservative 5+5 preset
  that has always been auto-curate's default.
- ``OVERHAUL_MIN_SCORE = 35``: below polish but still structurally a
  deck — the deliberate 15+15 revision preset. The 35 floor is the
  midpoint of the D/F grade bands (``deck_health._GRADE_BANDS``: D
  ends at 40, F below): a deck under ~35 is failing on multiple
  weighted components at once, which 15 swaps cannot fix.
- ``< 35 -> "rebuild"``: 30+30 through the same proposer / curator
  plumbing, plus an optional manabase rebuild via the FP-014 Karsten
  per-CMC model (``plan_manabase_rebuild`` below). Every change still
  flows through the normal legality + A/B verdict machinery — the tier
  only widens the budget, it never bypasses the empirical gate.

REBUILD IS OPT-IN, NOT AUTOMATIC (2026-08-17)
=============================================
The automatic escalation path (``--mode auto`` on auto-curate /
advise / improve, and the web audit's ``?mode=auto``) will NOT select
``rebuild`` on its own. It caps at ``overhaul`` — the next tier down —
and surfaces ``REBUILD_OPT_IN_NOTE`` instead.

Why: rebuild is a ~6x cost multiplier over polish (30+30 curated swaps,
each an LLM call and a Forge seat) and the ONLY thing standing between
the operator and that spend is the deck-health score. That score has
never been validated against sim outcomes — it is the same epistemic
class as CardScore, which failed three pre-registered gates (FP-015),
and this module's own opening paragraph is explicit that health carries
NO win-rate claim. An unvalidated score is allowed to pick between
timid and moderate budgets; it is not allowed to spend six times the
money by itself.

Two ways to opt in, both explicit:
  * ``--mode rebuild`` — the operator names the tier. Unaffected by any
    of this: explicit tier selection reads ``TIER_CAPS`` directly and
    never passes through ``resolve_tier``.
  * ``COMMANDER_BUILDER_REBUILD_TIER=1`` — lets AUTO escalate all the
    way to rebuild. Env-flag opt-in matching this repo's convention for
    unvalidated machinery (``COMMANDER_BUILDER_CARD_SCORE``,
    ``COMMANDER_BUILDER_CORPUS_NORMS``); it is set once per shell, so it
    also covers batch/improve runs where there is no per-round prompt.
    Remove the flag when the health score earns its escalation with a
    measured A/B result.

Score unavailable (health ``None`` — empty deck, Scryfall outage):
fall back to ``polish`` with a printed note, never crash. An outage is
not a deck-construction failure (the deck_health N/A contract), so it
must not trigger the aggressive tiers either.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

# --- thresholds (rationale in the module docstring) ------------------------
KEEP_MIN_SCORE = 75      # >= 75 -> keep   (0-2 swaps; bubble_analysis parity)
POLISH_MIN_SCORE = 55    # 55-74 -> polish (5+5; the long-standing default)
OVERHAUL_MIN_SCORE = 35  # 35-54 -> overhaul (15+15); < 35 -> rebuild (30+30)

# Per-tier (max_adds, max_cuts) caps. polish/overhaul/free values are
# THE existing ``commander-auto-curate --mode`` presets, imported here
# so there is one source of truth; keep mirrors bubble_analysis's
# "keep (0-2 swaps)" band; rebuild is the new widest tier.
TIER_CAPS: dict[str, tuple[int, int]] = {
    "keep":     (2,   2),
    "polish":   (5,   5),
    "overhaul": (15,  15),
    "rebuild":  (30,  30),
    "free":     (999, 999),  # effectively unbounded
}

# Tier used when the health score can't be computed (None). Polish is
# the conservative long-standing default — never escalate on missing
# data.
FALLBACK_TIER = "polish"

# Where automatic escalation stops without an explicit opt-in, and the
# env flag that lifts the stop (rationale in the module docstring).
AUTO_MAX_TIER = "overhaul"
REBUILD_TIER_ENV_VAR = "COMMANDER_BUILDER_REBUILD_TIER"

#: Printed / surfaced verbatim whenever auto WANTED rebuild and got
#: ``AUTO_MAX_TIER`` instead. Names both opt-in surfaces and the reason,
#: because "your budget was quietly capped" is useless without "here is
#: how to un-cap it, and here is why it was capped".
REBUILD_OPT_IN_NOTE = (
    "deck-health suggests a rebuild-tier budget (30+30); re-run with "
    "--mode rebuild, or set COMMANDER_BUILDER_REBUILD_TIER=1 to let "
    "auto escalate there — the health score gating this escalation is "
    "unvalidated. Capped at overhaul (15+15)."
)


def rebuild_tier_enabled() -> bool:
    """True when the operator has opted auto-escalation into ``rebuild``.

    Same truthy-value convention as ``card_score.is_enabled`` /
    ``_advisor_logging.is_enabled`` — one shared spelling of "is this
    flag on" across every opt-in in the codebase.
    """
    return os.environ.get(
        REBUILD_TIER_ENV_VAR, "",
    ).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class BudgetTier:
    """A resolved change-budget tier.

    ``fallback`` is True when the tier came from the None-score
    fallback (health unavailable) rather than an actual score — the
    caller must surface that in its printed note.

    ``rebuild_suppressed`` is True when the score mapped to ``rebuild``
    but the opt-in was absent, so ``mode`` is the capped
    ``AUTO_MAX_TIER`` instead. Callers surface ``REBUILD_OPT_IN_NOTE``
    when they see it (``format_auto_mode_line`` already does).
    """

    mode: str
    max_adds: int
    max_cuts: int
    health_score: Optional[int]
    fallback: bool = False
    rebuild_suppressed: bool = False


def resolve_tier(
    score: Optional[float], *, allow_rebuild: Optional[bool] = None,
) -> BudgetTier:
    """Map a deck-health score (0-100 or None) to a ``BudgetTier``.

    Boundary semantics (inclusive lower bounds): 75 -> keep, 74 ->
    polish, 55 -> polish, 54 -> overhaul, 35 -> overhaul. ``None`` ->
    the polish fallback with ``fallback=True``.

    Below 35 the score wants ``rebuild``, which this function only
    RETURNS when the operator has opted in (2026-08-17; see the module
    docstring). ``allow_rebuild=None`` (the default) reads
    ``COMMANDER_BUILDER_REBUILD_TIER`` at call time — env, not import,
    so a test or a caller can flip it mid-process. Pass the bool
    explicitly to bypass the env entirely. Without the opt-in the tier
    is capped at ``AUTO_MAX_TIER`` with ``rebuild_suppressed=True``;
    ``health_score`` still reports the real score, so nothing downstream
    has to reverse-engineer why the budget stopped where it did.
    """
    if score is None:
        adds, cuts = TIER_CAPS[FALLBACK_TIER]
        return BudgetTier(
            mode=FALLBACK_TIER, max_adds=adds, max_cuts=cuts,
            health_score=None, fallback=True,
        )
    s = int(score)
    suppressed = False
    if s >= KEEP_MIN_SCORE:
        mode = "keep"
    elif s >= POLISH_MIN_SCORE:
        mode = "polish"
    elif s >= OVERHAUL_MIN_SCORE:
        mode = "overhaul"
    else:
        if allow_rebuild is None:
            allow_rebuild = rebuild_tier_enabled()
        mode = "rebuild" if allow_rebuild else AUTO_MAX_TIER
        suppressed = not allow_rebuild
    adds, cuts = TIER_CAPS[mode]
    return BudgetTier(
        mode=mode, max_adds=adds, max_cuts=cuts, health_score=s,
        rebuild_suppressed=suppressed,
    )


def health_score_for_deck(deck_text: str) -> Optional[int]:
    """The deck's 0-100 health score, or None when it can't be computed.

    Thin fail-quiet wrapper around ``deck_health.compute_health_grade``
    — an outage or unexpected exception degrades to None (which
    ``resolve_tier`` maps to the polish fallback), never a crash. The
    grade's own 'N/A' contract already returns ``score: None`` for the
    all-signals-unavailable case.
    """
    try:
        from .deck_health import compute_health_grade
        grade = compute_health_grade(deck_text)
        score = grade.get("score")
        return int(score) if score is not None else None
    except Exception:  # noqa: BLE001 — budget resolution must not crash
        return None


def resolve_tier_for_deck(
    deck_text: str, *, allow_rebuild: Optional[bool] = None,
) -> BudgetTier:
    """Convenience: ``resolve_tier(health_score_for_deck(deck_text))``."""
    return resolve_tier(
        health_score_for_deck(deck_text), allow_rebuild=allow_rebuild,
    )


def format_auto_mode_line(tier: BudgetTier) -> str:
    """The one-line disclosure every ``--mode auto`` surface prints.

    A suppressed rebuild appends ``REBUILD_OPT_IN_NOTE`` — the operator
    asked the score to size the budget, so they are owed the fact that
    it wanted more than it was allowed to take.
    """
    if tier.fallback:
        return (
            f"auto mode: {tier.mode} (health score unavailable — "
            f"defaulting to {FALLBACK_TIER})"
        )
    line = f"auto mode: {tier.mode} (health {tier.health_score}/100)"
    if tier.rebuild_suppressed:
        # One line, not two: callers indent this string differently
        # (auto-curate pads it, advise doesn't), so a newline inside it
        # would land ragged on one of them.
        line = f"{line} — {REBUILD_OPT_IN_NOTE}"
    return line


def suggested_mode_payload(score: Optional[float]) -> dict:
    """JSON-shaped tier suggestion for the web audit payload.

    Always returns a dict (never raises): ``{"mode": str,
    "health_score": int|None, "fallback": bool, "rebuild_suppressed":
    bool, "note": str|None}``. The UI renders it next to the
    health-grade tile as "suggested mode: <tier> (health N/100)".

    ``note`` is non-None only when the score wanted ``rebuild`` and the
    opt-in was absent (2026-08-17); it carries the same text the CLI
    prints, so the API surface and the terminal tell the operator the
    same story about a capped budget.
    """
    tier = resolve_tier(score)
    return {
        "mode": tier.mode,
        "health_score": tier.health_score,
        "fallback": tier.fallback,
        "rebuild_suppressed": tier.rebuild_suppressed,
        "note": REBUILD_OPT_IN_NOTE if tier.rebuild_suppressed else None,
    }


def trim_recommendations(recommendations, max_adds: int, max_cuts: int):
    """Cap a SwapRecommendation list to the tier budget.

    Keeps the first ``max_adds`` adds and first ``max_cuts`` cuts in
    their original order (the recommenders emit rank-ordered lists, so
    "first N" is "top N"). Non-add/cut actions pass through untouched.
    Returns a NEW list; the input is not mutated.
    """
    kept: list = []
    adds_kept = cuts_kept = 0
    for rec in recommendations:
        action = getattr(rec, "action", None)
        if action == "add":
            if adds_kept >= max_adds:
                continue
            adds_kept += 1
        elif action == "cut":
            if cuts_kept >= max_cuts:
                continue
            cuts_kept += 1
        kept.append(rec)
    return kept


# ---------------------------------------------------------------------------
# Rebuild tier: optional Karsten manabase rebuild
# ---------------------------------------------------------------------------

def plan_manabase_rebuild(
    deck_text: str, *, lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[dict]:
    """Plan land swaps that rebuild an existing deck's manabase via the
    FP-014 Karsten per-CMC model.

    Reuses ``deck_builder_manabase`` exactly the way commander-build
    does — ``pip_stats`` over the deck's own nonland spells feeds
    ``build_manabase``, with the deck's CURRENT nonbasic lands passed
    as the kept seed (a tuned dual/fetch base is respected; the
    rebuild's work is topping up missing fixers and reallocating
    basics toward the Karsten per-color deficits).

    DELIBERATELY LAND-COUNT-NEUTRAL: the budget is the deck's current
    land count, and the returned ``adds`` / ``cuts`` are trimmed to
    equal length, so staging them as swap pairs through the normal
    legality path (``apply_proposal_to_deck`` ->
    ``_apply_swaps_to_dck``) can only change the land MIX, never the
    mainboard size. Changing the land COUNT means swapping spells for
    lands — that stays the curator's job through its normal budget.

    Returns ``{"adds": [name, ...], "cuts": [name, ...],
    "summary": [line, ...]}`` (adds/cuts one entry per copy, equal
    lengths, possibly empty), or ``None`` when no plan can be made:
    unparseable deck, majority-lookup outage (the manabase_report
    contract — never rebuild on data we don't have), unresolvable
    color identity, or a landless deck.

    ``lookup`` is injectable for offline tests; defaults to the
    disk-cached Scryfall client via ``deck_builder_manabase``'s own
    default resolver.
    """
    from collections import Counter

    from .dck_utils import iter_section_lines, parse_card_line
    from .deck_builder_manabase import (
        _WUBRG,
        _default_lookup,
        build_manabase,
        pip_stats,
    )
    from .deck_library_analyzer import iter_deck_cards
    from .staples import is_basic_land

    resolve = lookup or _default_lookup
    cache: dict[str, Optional[dict]] = {}

    def _cached(name: str) -> Optional[dict]:
        key = name.strip().lower()
        if key not in cache:
            try:
                cache[key] = resolve(name)
            except Exception:  # noqa: BLE001 — a failed lookup is a miss
                cache[key] = None
        return cache[key]

    entries = [(qty, name) for qty, name in iter_deck_cards(deck_text) if name]
    if not entries:
        return None
    failed = sum(1 for _qty, name in entries if _cached(name) is None)
    if failed * 2 > len(entries):
        return None  # outage contract: never rebuild on majority-missing data

    commander_names = [
        parsed[1]
        for line in iter_section_lines(deck_text, "Commander")
        for parsed in [parse_card_line(line)]
        if parsed and parsed[1]
    ]

    def _identity_of(names) -> set[str]:
        out: set[str] = set()
        for nm in names:
            card = _cached(nm) or {}
            out |= {
                c.upper() for c in (card.get("color_identity") or [])
                if isinstance(c, str) and c.upper() in _WUBRG
            }
        return out

    identity = _identity_of(commander_names)
    if not identity:
        identity = _identity_of(nm for _qty, nm in entries)
    if not identity:
        # Unresolvable identity: build_manabase would degrade to an
        # all-Wastes base — proposing that as a "rebuild" of a colored
        # deck would be nonsense, so decline to plan instead.
        return None

    # Lands vs nonlands, front face deciding (deck_health's land-walk
    # convention: a "Sorcery // Land" MDFC is a spell).
    lands: list[tuple[int, str]] = []
    nonlands: list[tuple[int, str]] = []
    for qty, name in entries:
        card = _cached(name) or {}
        type_line = card.get("type_line") or ""
        if not type_line:
            faces = card.get("card_faces") or []
            if faces:
                type_line = " // ".join(
                    (f or {}).get("type_line") or "" for f in faces
                )
        front = type_line.split("//")[0].lower()
        if "land" in front or (not type_line and is_basic_land(name)):
            lands.append((qty, name))
        else:
            nonlands.append((qty, name))

    land_count = sum(qty for qty, _nm in lands)
    if land_count <= 0:
        return None

    stats = pip_stats([nm for _qty, nm in nonlands], _cached)
    colors = [c for c in _WUBRG if c in identity]
    kept_seed = [nm for _qty, nm in lands if not is_basic_land(nm)]

    manabase = build_manabase(
        colors, [nm for _qty, nm in nonlands], kept_seed, land_count,
        lookup=_cached, stats=stats,
    )
    if manabase.summary.degraded:
        return None

    # Multiset diff, keyed case-insensitively with display names kept.
    display: dict[str, str] = {}
    current: Counter = Counter()
    for qty, nm in lands:
        key = nm.strip().lower()
        display.setdefault(key, nm)
        current[key] += qty
    proposed: Counter = Counter()
    for nm in manabase.lands:
        key = nm.strip().lower()
        display.setdefault(key, nm)
        proposed[key] += 1
    for nm, qty in manabase.basics.items():
        key = nm.strip().lower()
        display.setdefault(key, nm)
        proposed[key] += qty

    adds: list[str] = []
    cuts: list[str] = []
    # Adds in the manabase's own build order (fixers before basics);
    # cuts in deck order. Both deterministic.
    for nm in list(manabase.lands) + list(manabase.basics):
        key = nm.strip().lower()
        adds.extend([display[key]] * max(0, proposed[key] - current[key]))
    for _qty, nm in lands:
        key = nm.strip().lower()
        surplus = max(0, current[key] - proposed[key])
        if surplus:
            cuts.extend([display[key]] * surplus)
            current[key] -= surplus  # consume so later lines don't re-cut
    # Balance to equal length so the staged pairs are size-neutral (the
    # legality path would balance anyway; doing it here keeps the plan
    # honest about what will land).
    n = min(len(adds), len(cuts))
    return {
        "adds": adds[:n],
        "cuts": cuts[:n],
        "summary": manabase.summary.format_lines(),
    }
