"""FP-019.2 — named consistency targets from the 40-primer synthesis.

WHAT THIS IS
============
The primer harvest's §1 numbers converged hard enough across independent
authors to use as program floors: hit the 3rd land drop ~85% of the
time, cast the commander on curve in its colors ~85%+, find card
advantage by turn 5 ~90%+, and — for decks whose plan NEEDS a turn-1
mana enabler — run 13 of them and count the free mulligan into the math.
This module turns those figures into ONE documented table
(:data:`CONSISTENCY_TARGETS`) and one evaluator that grades a deck
against it, reusing the math that already exists: the Monte-Carlo
projection ``deck_health.consistency_signal`` computes anyway, and
``consistency.hypergeom_at_least`` for the closed-form checks.

CONDITIONAL TARGETS ARE REPORTED, NOT JUDGED
============================================
Two of the five floors only bind under a declared plan (§1): the
13-enabler rule assumes a T1-enabler strategy, and the ≤2
tapped-fetchables rule assumes a proactive T1–2 curve-out. The evaluator
cannot know a deck's plan from its list, so without the plan flag those
checks report their VALUE with ``applies: False`` and ``met: None`` —
information for the tile, never a verdict. Callers that do know the plan
(a future intent/archetype classifier, or a CLI flag) pass ``plans=``.

ADDITIVE ONLY (the deck_health doctrine)
========================================
This ships as a REPORTED deck-health tile. It is deliberately NOT an
input to ``compute_health_grade`` — folding it in would silently
re-grade every deck the day it lands (the exact drift the grade's
pinned calibration tests exist to prevent). If it ever earns a grade
weight, that is its own reviewed, test-repinned change.

OUTAGE CONTRACT (mirrors deck_health / consistency)
===================================================
``None`` for an empty deck or a majority-of-lookups failure; a check
whose input is unavailable reports ``met: None`` — never a fabricated
pass or fail.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from . import dck_utils
from .consistency import OPENING_HAND_SIZE, hypergeom_at_least
from .staples import classify_role

#: Cards seen by the end of turn 5 ON THE PLAY: the opening 7 plus the
#: four draw steps of turns 2–5 (no draw on turn 1 on the play). The
#: harsher convention, matching ``consistency.py``'s default.
CARDS_SEEN_BY_T5_ON_PLAY = OPENING_HAND_SIZE + 4

#: The primer-derived floors (§1 of deckbuilding_heuristics.md). Each
#: entry: ``target`` + comparison ``op``, an optional ``plan`` gate for
#: the conditional rules, and the primer that derived the number — the
#: table is the whole tuning surface, same pattern as
#: ``bracket_estimator.DEFAULT_WEIGHTS``.
CONSISTENCY_TARGETS: dict[str, dict] = {
    "third_land_drop": {
        "target": 0.85, "op": ">=", "plan": None,
        "cite": "Edgar: 40 lands in a MV<3 deck = 85.27%; missing the 3rd "
                "drop 'significantly reduces win probability'",
    },
    "commander_on_curve": {
        "target": 0.85, "op": ">=", "plan": None,
        "cite": "Henzie benchmark transplant: 85–95% band (cEDH Yuriko "
                "95%, Modern 3-color ~85%); floor of the band",
    },
    "card_advantage_by_t5": {
        "target": 0.90, "op": ">=", "plan": None,
        "cite": "Edgar: 15 draw slots + 3 tutors ≈ 92% to see one by T5",
    },
    "t1_enablers": {
        "target": 13, "op": ">=", "plan": "t1_enabler_plan",
        "cite": "Henzie/Yuriko standard: 13 enablers = 63.9% opener, 87% "
                "counting the free mulligan; count the mulligan in",
    },
    "tapped_fetchables": {
        "target": 2, "op": "<=", "plan": "proactive_t2_plan",
        "cite": "Henzie: 4 tapped-but-fetchable lands = 26% one starts in "
                "hand; proactive T1–2 decks run at most 1–2",
    },
}

#: Basic land subtypes — a land is FETCHABLE (by the common typed
#: fetch/slow-fetch class) when its type line carries one of these.
_BASIC_TYPES = ("Plains", "Island", "Swamp", "Mountain", "Forest")

#: Unconditionally-enters-tapped detector. The "unless" / "you may pay"
#: exclusion keeps conditional lands (shocks, checks) out: §1's opener
#: contamination argument only applies to lands that are ALWAYS tapped.
_TAPPED_RE = re.compile(r"enters (?:the battlefield )?tapped", re.I)
_CONDITIONAL_RE = re.compile(r"\bunless\b|you may pay", re.I)


def _oracle_of(card: dict) -> str:
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces")
    if isinstance(faces, list):
        return "\n".join(f.get("oracle_text") or "" for f in faces)
    return ""


def _is_tapped_fetchable(card: dict) -> bool:
    type_line = card.get("type_line") or ""
    if "Land" not in type_line:
        return False
    if not any(b in type_line for b in _BASIC_TYPES):
        return False
    oracle = _oracle_of(card)
    return bool(_TAPPED_RE.search(oracle)) and not _CONDITIONAL_RE.search(oracle)


def _is_t1_enabler(card: dict) -> bool:
    """A turn-1 mana enabler: nonland, MV ≤ 1, ramp-classified (dorks,
    rituals, Sol-Ring-class rocks — whatever adds mana ahead of curve)."""
    type_line = card.get("type_line") or ""
    if "Land" in type_line:
        return False
    cmc = card.get("cmc")
    if cmc is None or cmc > 1:
        return False
    return classify_role(_oracle_of(card), type_line) == "ramp"


def _check(value, key: str, applies: bool) -> dict:
    spec = CONSISTENCY_TARGETS[key]
    met: Optional[bool] = None
    if value is not None and applies:
        met = (value >= spec["target"]) if spec["op"] == ">=" \
            else (value <= spec["target"])
    return {"value": value, "target": spec["target"], "op": spec["op"],
            "applies": applies, "met": met}


def evaluate_consistency_targets(
    deck_text: str,
    *,
    consistency: Optional[dict] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    plans: Optional[set] = None,
) -> Optional[dict]:
    """Grade ``deck_text`` against :data:`CONSISTENCY_TARGETS`.

    ``consistency`` is the projection ``deck_health.consistency_signal``
    already computes (only ``p_3_lands_by_t3`` and
    ``p_commander_on_curve`` are read) — passing it avoids a second
    Monte-Carlo run; ``None`` leaves those two checks unjudged.
    ``plans`` declares which conditional rules bind (see module
    docstring). ``lookup`` is Scryfall-shaped; defaults to the same
    disk-cached safe path every deck_health signal uses.
    """
    quantities = dck_utils.main_card_quantities(deck_text)
    if not quantities:
        return None
    if lookup is None:
        from .deck_health import _lookup_card_safe as lookup  # noqa: N813

    plan_set = plans or set()
    deck_size = sum(quantities.values())
    lookup_failures = 0
    draw_tutor_slots = 0
    t1_enablers = 0
    tapped_fetchables = 0
    for name, qty in quantities.items():
        card = lookup(name)
        if card is None:
            lookup_failures += 1
            continue
        role = classify_role(_oracle_of(card), card.get("type_line") or "")
        if role in ("draw", "tutor"):
            draw_tutor_slots += qty
        if _is_t1_enabler(card):
            t1_enablers += qty
        if _is_tapped_fetchable(card):
            tapped_fetchables += qty
    if lookup_failures * 2 > len(quantities):
        return None  # the standard majority-failure outage guard

    p_card_advantage = hypergeom_at_least(
        deck_size, draw_tutor_slots, CARDS_SEEN_BY_T5_ON_PLAY, 1,
    )
    p_enabler_opener = hypergeom_at_least(
        deck_size, t1_enablers, OPENING_HAND_SIZE, 1,
    )
    # CR 103.5c: the first mulligan is free, so the honest odds of a T1
    # enabler are over TWO independent openers (§1: "count the mulligan
    # into the math" — 13 enablers: 63.9% raw, 87% with the free mull).
    p_enabler_with_mull = None if p_enabler_opener is None \
        else 1.0 - (1.0 - p_enabler_opener) ** 2

    proj = consistency or {}
    checks = {
        "third_land_drop": _check(
            proj.get("p_3_lands_by_t3"), "third_land_drop", True),
        "commander_on_curve": _check(
            proj.get("p_commander_on_curve"), "commander_on_curve", True),
        "card_advantage_by_t5": _check(
            p_card_advantage, "card_advantage_by_t5", True),
        "t1_enablers": {
            **_check(t1_enablers, "t1_enablers",
                     "t1_enabler_plan" in plan_set),
            "p_with_free_mulligan": p_enabler_with_mull,
        },
        "tapped_fetchables": _check(
            tapped_fetchables, "tapped_fetchables",
            "proactive_t2_plan" in plan_set),
    }
    applicable = [c for c in checks.values() if c["met"] is not None]
    return {
        "checks": checks,
        "met": sum(1 for c in applicable if c["met"]),
        "evaluated": len(applicable),
        "lookup_failures": lookup_failures,
    }
