"""FP-019.5 — nonbo lint: the §14 self-conflict table as pairwise checks.

WHAT THIS IS
============
The primer harvest's §14 table collects ANTI-synergies real deck authors
documented the hard way: your own anthem turning Skullclamp off, Cursed
Totem shutting down your own dorks, a forced-draw engine letting
opponents kill your Thassa's Oracle win. EDHREC inclusion rates can
never surface these (they are *notable-exclusion* knowledge), and
nothing else in the tree checks a deck against itself. This module is
that check: a data-driven rule table + one linter that reports which
rules fire on a deck.

RULE SHAPE
==========
Each rule has selector ``a`` and (usually) selector ``b``; the rule
fires when the deck holds at least one card matching EACH side (a card
may not satisfy both sides of one rule by itself). A selector matches by
exact ``names`` OR by predicates over card data (``oracle_re`` /
``type_re`` / ``toughness``). Single-selector rules (symmetric-effect
taxes like Coat of Arms) fire on presence alone at severity ``note``.

Severities: ``warn`` — the pair actively works against the deck;
``note`` — a documented cost worth surfacing, not necessarily a cut.

WHAT THIS IS NOT
================
Not a combo detector (``combo_detection`` owns positive lines), not a
judgment of card quality, and not exhaustive — §14's own-stax-vs-own-
combo class needs combo-line awareness and stays future work. The
commander-dependence check from the same table ships as a CardScore
penalty (FP-019.4), not here, because it ranks candidates rather than
linting a finished list.

OFFLINE / FAIL-QUIET
====================
All card data routes through an injected Scryfall-shaped ``lookup``
(default: deck_health's disk-cached safe path). A card that fails to
resolve simply matches nothing — a Scryfall outage degrades the lint to
silence, never to a crash or a fabricated finding.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from . import dck_utils

#: The §14 table, transcribed with each rule citing the primer that
#: documented it. Names are exact Scryfall names; regexes run over
#: lowercased oracle text / type lines.
NONBO_RULES: tuple[dict, ...] = (
    {
        "id": "skullclamp_vs_anthems",
        "severity": "warn",
        "a": {"names": ("Skullclamp",)},
        "b": {"oracle_re": r"creatures you control get \+1/\+1"},
        "why": "Your own +1/+1 anthem pushes toughness past 1, so "
               "Skullclamp can no longer convert a creature into two "
               "cards.",
        "source": "Lathril primer",
    },
    {
        "id": "deathreap_vs_end_step_deaths",
        "severity": "note",
        "a": {"names": ("Deathreap Ritual",)},
        "b": {"oracle_re": r"\b(?:blitz|dash)\b"},
        "why": "Blitz/dash bodies die AT the beginning of the end step — "
               "too late for 'died this turn' end-step checks to have "
               "seen a death before the trigger window closes on "
               "opponents' turns.",
        "source": "Henzie primer",
    },
    {
        "id": "cursed_totem_vs_own_dorks",
        "severity": "warn",
        "a": {"names": ("Cursed Totem",)},
        "b": {"oracle_re": r"\{t\}: add", "type_re": r"creature"},
        "why": "Cursed Totem shuts off your own creatures' activated "
               "abilities — never include it when your mana or combo "
               "lines run through them.",
        "source": "Winota-stax / Urza primers",
    },
    {
        "id": "wheels_vs_mass_bounce",
        "severity": "note",
        "a": {"names": ("Windfall", "Echo of Eons", "Time Reversal")},
        "b": {"names": ("Thing in the Ice // Awoken Horror",
                        "Awoken Horror")},
        "why": "The mass bounce fills opponents' hands and the wheel "
               "refills them (and feeds their graveyards) — the two "
               "halves undo each other's damage.",
        "source": "Baral primer",
    },
    {
        "id": "forced_draw_vs_thassas_oracle",
        "severity": "warn",
        "a": {"names": ("Thassa's Oracle",)},
        "b": {"names": ("Esper Sentinel", "Archivist of Oghma")},
        "why": "Opponents can pay into your forced-draw effect to empty "
               "your library out from under your own Oracle trigger — "
               "Silence first or sacrifice the source before comboing.",
        "source": "Najeela primer",
    },
    {
        "id": "coat_of_arms_symmetry",
        "severity": "note",
        "a": {"names": ("Coat of Arms",)},
        "b": None,
        "why": "Symmetric anthem: every tribal opponent at the table "
               "gets the same boost — model the self-cost before "
               "keeping it.",
        "source": "Lathril primer",
    },
    {
        "id": "ruthless_winnower_tax",
        "severity": "note",
        "a": {"names": ("Ruthless Winnower",)},
        "b": None,
        "why": "Mandatory symmetric sacrifice taxes your own non-Elf "
               "pieces every turn.",
        "source": "Lathril primer",
    },
    {
        "id": "heartless_summoning_vs_x1_creatures",
        "severity": "warn",
        "a": {"names": ("Heartless Summoning",)},
        "b": {"type_re": r"creature", "toughness": "1"},
        "why": "The blanket -1/-1 kills your own one-toughness creatures "
               "on arrival.",
        "source": "Henzie primer (game notes)",
    },
    {
        "id": "asceticism_vs_everlasting_torment",
        "severity": "warn",
        "a": {"names": ("Asceticism",)},
        "b": {"names": ("Everlasting Torment",)},
        "why": "Mode-exclusive engines: Asceticism's regeneration is "
               "dead under Everlasting Torment's no-regeneration clause "
               "— tutor for whichever mode is live, never run both "
               "blind.",
        "source": "Auntie Ool primer",
    },
    {
        "id": "own_shroud_vs_own_targeting",
        "severity": "note",
        "a": {"oracle_re": r"(?:creatures you control|enchanted creature) "
                           r"(?:has|have|gains?) shroud"},
        "b": {"oracle_re": r"target creature you control"},
        "why": "Shroud you grant blocks your OWN targeted pumps and "
               "auras too (attach-without-targeting effects are the "
               "rare exception).",
        "source": "Pako / Light-Paws primers",
    },
)


def _match(selector: Optional[dict], name: str,
           card: Optional[dict]) -> bool:
    """Does ``name``/``card`` satisfy ``selector``?

    Exact names need no card data; predicate selectors silently fail to
    match when the card could not be resolved (fail-quiet contract).
    """
    if selector is None:
        return False
    names = selector.get("names")
    if names is not None:
        return name in names
    if card is None:
        return False
    oracle = (card.get("oracle_text") or "").lower()
    type_line = (card.get("type_line") or "").lower()
    oracle_re = selector.get("oracle_re")
    if oracle_re is not None and not re.search(oracle_re, oracle):
        return False
    type_re = selector.get("type_re")
    if type_re is not None and not re.search(type_re, type_line):
        return False
    toughness = selector.get("toughness")
    if toughness is not None and card.get("toughness") != toughness:
        return False
    return True


def lint_cards(
    card_names,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[dict]:
    """Run every :data:`NONBO_RULES` rule over ``card_names``.

    Returns fired rules, ``warn`` first:
    ``{"rule", "severity", "cards_a", "cards_b", "why", "source"}``.
    ``cards_b`` is empty for single-selector rules. A card never
    satisfies both sides of one rule by itself.
    """
    if lookup is None:
        from .deck_health import _lookup_card_safe as lookup  # noqa: N813

    names = list(dict.fromkeys(n for n in card_names if n))
    cards: dict[str, Optional[dict]] = {}
    for n in names:
        try:
            cards[n] = lookup(n)
        except Exception:  # noqa: BLE001 — lint must degrade, not crash
            cards[n] = None

    findings: list[dict] = []
    for rule in NONBO_RULES:
        hits_a = [n for n in names if _match(rule["a"], n, cards[n])]
        if not hits_a:
            continue
        if rule["b"] is None:
            findings.append({
                "rule": rule["id"], "severity": rule["severity"],
                "cards_a": hits_a, "cards_b": [],
                "why": rule["why"], "source": rule["source"],
            })
            continue
        hits_b = [n for n in names
                  if n not in hits_a and _match(rule["b"], n, cards[n])]
        if hits_b:
            findings.append({
                "rule": rule["id"], "severity": rule["severity"],
                "cards_a": hits_a, "cards_b": hits_b,
                "why": rule["why"], "source": rule["source"],
            })
    findings.sort(key=lambda f: (f["severity"] != "warn", f["rule"]))
    return findings


def lint_deck_text(
    deck_text: str,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[dict]:
    """:func:`lint_cards` over a ``.dck`` blob's [Commander] + [Main]."""
    names = dck_utils.section_card_names(deck_text, "Commander") \
        + dck_utils.main_card_names(deck_text)
    return lint_cards(names, lookup=lookup)
