"""Bug-fix coverage for the advisor cut path:

1. Tribal-theme bypass -- when the commander has a creature subtype that
   its oracle text references, cards sharing that subtype must NOT be
   recommended for cut just because EDHREC's commander page didn't list
   them (false positives like Glorious Protector in a Giada angel deck).
2. Reason wording -- the cut reason now reads as an off-theme signal,
   not a card-quality claim.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from commander_builder import _advisor_heuristic as ah
from commander_builder.edhrec_client import CardEntry, CommanderPage


def _giada_page(top_cards):
    return CommanderPage(
        commander_name="Giada, Font of Hope",
        slug="giada-font-of-hope",
        fetched_at="2026-05-28T00:00:00Z",
        top_cards=[CardEntry(name=n, inclusion_pct=80, num_decks=1000)
                   for n in top_cards],
    )


@pytest.fixture
def fake_scryfall_cache(monkeypatch):
    """Replace the cache-only Scryfall lookup with an in-memory map so we
    can exercise the tribal helpers without touching the filesystem."""
    db: dict[str, dict] = {
        "Giada, Font of Hope": {
            "type_line": "Legendary Creature — Angel",
            "oracle_text": "Angels you control enter the battlefield with "
                           "an additional +1/+1 counter on them.",
        },
        "Glorious Protector": {
            "type_line": "Creature — Angel",
            "oracle_text": "...",
        },
        "Solitude": {
            "type_line": "Creature — Elemental Incarnation",
            "oracle_text": "...",
        },
        "Krenko, Mob Boss": {
            "type_line": "Legendary Creature — Goblin Warrior",
            "oracle_text": "Tap: Create X 1/1 red Goblin creature tokens.",
        },
        "Atraxa, Praetors' Voice": {
            "type_line": "Legendary Creature — Angel Horror",
            "oracle_text": "Flying, vigilance. At the beginning of your end "
                           "step, proliferate.",
        },
    }
    monkeypatch.setattr(ah, "_cached_scryfall", lambda name: db.get(name))
    return db


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_commander_tribal_subtype_detects_angel(fake_scryfall_cache):
    assert ah._commander_tribal_subtype("Giada, Font of Hope") == "Angel"


def test_commander_tribal_subtype_detects_goblin_from_multi_subtype(fake_scryfall_cache):
    # Krenko is "Goblin Warrior"; the oracle text references Goblin tokens
    # (not Warrior). Detection must pick the referenced subtype.
    assert ah._commander_tribal_subtype("Krenko, Mob Boss") == "Goblin"


def test_commander_tribal_subtype_none_when_not_referenced(fake_scryfall_cache):
    # Atraxa's oracle text never mentions Angel or Horror -- not tribal.
    assert ah._commander_tribal_subtype("Atraxa, Praetors' Voice") is None


def test_commander_tribal_subtype_none_on_cache_miss(fake_scryfall_cache):
    assert ah._commander_tribal_subtype("Some Unknown Commander") is None


def test_card_has_subtype_positive_and_negative(fake_scryfall_cache):
    assert ah._card_has_subtype("Glorious Protector", "Angel")
    assert not ah._card_has_subtype("Solitude", "Angel")
    # Cache miss falls through to False -- never a false positive.
    assert not ah._card_has_subtype("Unknown Card", "Angel")


# ---------------------------------------------------------------------------
# End-to-end: the cut loop honors the bypass + emits the new reason
# ---------------------------------------------------------------------------

def test_cut_skips_in_tribe_card_for_tribal_commander(fake_scryfall_cache):
    """Glorious Protector is an Angel; Giada is the angel commander; the
    cut loop must NOT recommend cutting Glorious Protector even though
    EDHREC's Giada page omits it."""
    # Need >= MIN_EDHREC_SIGNAL_FOR_CUTS top_cards for the safety net to
    # release; pad with filler so cuts actually run.
    page = _giada_page([f"Filler Card {i}" for i in range(60)])
    deck = {"Glorious Protector", "Sol Ring"}  # Sol Ring is a universal staple
    recs = ah._heuristic_swap_recommendations(
        deck_cards=deck, edhrec_page=page,
    )
    cuts = [r for r in recs if r.action == "cut"]
    cut_names = {r.card for r in cuts}
    assert "Glorious Protector" not in cut_names, (
        "Angel must not be cut from an Angel-tribal deck"
    )


def test_cut_still_fires_for_non_tribal_card(fake_scryfall_cache):
    """Same Giada setup but a non-Angel utility card (Solitude). The cut
    path SHOULD still fire -- the bypass is type-shape-specific."""
    page = _giada_page([f"Filler Card {i}" for i in range(60)])
    deck = {"Solitude"}
    recs = ah._heuristic_swap_recommendations(
        deck_cards=deck, edhrec_page=page,
    )
    cuts = [r for r in recs if r.action == "cut"]
    assert any(r.card == "Solitude" for r in cuts), (
        "Solitude is non-Angel utility -- the cut path should still flag it"
    )


def test_cut_reason_wording_is_off_theme_phrasing(fake_scryfall_cache):
    """Reason now reads as an off-theme signal, not a quality claim."""
    page = _giada_page([f"Filler Card {i}" for i in range(60)])
    recs = ah._heuristic_swap_recommendations(
        deck_cards={"Solitude"}, edhrec_page=page,
    )
    cuts = [r for r in recs if r.action == "cut" and r.card == "Solitude"]
    assert cuts, "expected a Solitude cut"
    assert "off-theme" in cuts[0].reason
    assert "EDHREC top/high-synergy" in cuts[0].reason


def test_cut_unaffected_when_commander_is_not_tribal(fake_scryfall_cache):
    """Atraxa: not tribal. Bypass returns None; cuts behave as before."""
    page = CommanderPage(
        commander_name="Atraxa, Praetors' Voice",
        slug="atraxa",
        fetched_at="2026-05-28T00:00:00Z",
        top_cards=[CardEntry(name=f"Filler {i}", inclusion_pct=80,
                             num_decks=1000) for i in range(60)],
    )
    recs = ah._heuristic_swap_recommendations(
        deck_cards={"Glorious Protector"}, edhrec_page=page,
    )
    cuts = [r for r in recs if r.action == "cut"]
    assert any(r.card == "Glorious Protector" for r in cuts), (
        "Atraxa isn't an Angel-tribal commander -- the bypass should NOT trigger"
    )
