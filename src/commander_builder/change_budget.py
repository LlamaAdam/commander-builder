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
- ``< 35 -> "rebuild"`` (NEW tier): 30+30 through the same proposer /
  curator plumbing, plus an optional manabase rebuild via the FP-014
  Karsten per-CMC model (``plan_manabase_rebuild`` below). Every
  change still flows through the normal legality + A/B verdict
  machinery — the tier only widens the budget, it never bypasses the
  empirical gate.

Score unavailable (health ``None`` — empty deck, Scryfall outage):
fall back to ``polish`` with a printed note, never crash. An outage is
not a deck-construction failure (the deck_health N/A contract), so it
must not trigger the aggressive tiers either.
"""
from __future__ import annotations

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


@dataclass(frozen=True)
class BudgetTier:
    """A resolved change-budget tier.

    ``fallback`` is True when the tier came from the None-score
    fallback (health unavailable) rather than an actual score — the
    caller must surface that in its printed note.
    """

    mode: str
    max_adds: int
    max_cuts: int
    health_score: Optional[int]
    fallback: bool = False


def resolve_tier(score: Optional[float]) -> BudgetTier:
    """Map a deck-health score (0-100 or None) to a ``BudgetTier``.

    Boundary semantics (inclusive lower bounds): 75 -> keep, 74 ->
    polish, 55 -> polish, 54 -> overhaul, 35 -> overhaul, 34 ->
    rebuild. ``None`` -> the polish fallback with ``fallback=True``.
    """
    if score is None:
        adds, cuts = TIER_CAPS[FALLBACK_TIER]
        return BudgetTier(
            mode=FALLBACK_TIER, max_adds=adds, max_cuts=cuts,
            health_score=None, fallback=True,
        )
    s = int(score)
    if s >= KEEP_MIN_SCORE:
        mode = "keep"
    elif s >= POLISH_MIN_SCORE:
        mode = "polish"
    elif s >= OVERHAUL_MIN_SCORE:
        mode = "overhaul"
    else:
        mode = "rebuild"
    adds, cuts = TIER_CAPS[mode]
    return BudgetTier(
        mode=mode, max_adds=adds, max_cuts=cuts, health_score=s,
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


def resolve_tier_for_deck(deck_text: str) -> BudgetTier:
    """Convenience: ``resolve_tier(health_score_for_deck(deck_text))``."""
    return resolve_tier(health_score_for_deck(deck_text))


def format_auto_mode_line(tier: BudgetTier) -> str:
    """The one-line disclosure every ``--mode auto`` surface prints."""
    if tier.fallback:
        return (
            f"auto mode: {tier.mode} (health score unavailable — "
            f"defaulting to {FALLBACK_TIER})"
        )
    return f"auto mode: {tier.mode} (health {tier.health_score}/100)"


def suggested_mode_payload(score: Optional[float]) -> dict:
    """JSON-shaped tier suggestion for the web audit payload.

    Always returns a dict (never raises): ``{"mode": str,
    "health_score": int|None, "fallback": bool}``. The UI renders it
    next to the health-grade tile as "suggested mode: <tier>
    (health N/100)".
    """
    tier = resolve_tier(score)
    return {
        "mode": tier.mode,
        "health_score": tier.health_score,
        "fallback": tier.fallback,
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
