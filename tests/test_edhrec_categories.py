"""Tests for the Scryfall type_line → EDHREC section fallback used by
the average-deck preview helper.

Background: ``project_average_deck_preview`` surfaces each average-deck
entry's category via the ``edhrec_categories`` map the advisor builds
from the commander page's ``category_lists``. Roughly 21% of preview
cards weren't bucketed by EDHREC and fell into the UI's 'Other' pile
(TIER-2.1 punch-list item). The advisor now back-fills those entries
from Scryfall's ``type_line`` so the UI groups them under the
appropriate section (Creatures, Lands, Instants, ...).

Tests cover two layers:
1. ``_category_from_type_line`` — pure mapping from type_line to
   section header, with priority handling for compound types like
   ``Artifact Creature`` (→ Creatures).
2. ``_enrich_edhrec_categories_from_scryfall`` — the enrichment pass
   that walks ``average_deck.cards`` and fills holes via injected
   ``lookup_fn`` (Scryfall call by default; mocked in tests).
"""
from __future__ import annotations

import pytest

from commander_builder.edhrec_client import AverageDeck, CardEntry
from commander_builder.improvement_advisor import (
    _category_from_type_line,
    _enrich_edhrec_categories_from_scryfall,
)


# ---------------------------------------------------------------------------
# _category_from_type_line: priority-ordered string match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "type_line,expected",
    [
        ("Creature — Human Wizard", "Creatures"),
        ("Legendary Creature — Goblin", "Creatures"),
        # Artifact Creature buckets to Creatures, matching EDHREC.
        ("Artifact Creature — Construct", "Creatures"),
        ("Enchantment Creature — Boar", "Creatures"),
        ("Land", "Lands"),
        ("Basic Land — Forest", "Lands"),
        ("Land — Mountain", "Lands"),
        # Legendary Land still buckets to Lands.
        ("Legendary Land — Urza's", "Lands"),
        ("Instant", "Instants"),
        ("Sorcery", "Sorceries"),
        ("Enchantment", "Enchantments"),
        ("Legendary Enchantment — Saga", "Enchantments"),
        ("Planeswalker — Jace", "Planeswalkers"),
        ("Legendary Planeswalker — Jace", "Planeswalkers"),
        ("Battle — Siege", "Battles"),
        ("Artifact", "Artifacts"),
        ("Legendary Artifact — Equipment", "Artifacts"),
    ],
)
def test_category_from_type_line_known_types(type_line, expected):
    assert _category_from_type_line(type_line) == expected


@pytest.mark.parametrize("type_line", ["", "Tribal — Goblin", "Phenomenon"])
def test_category_from_type_line_unknown_returns_none(type_line):
    assert _category_from_type_line(type_line) is None


# ---------------------------------------------------------------------------
# _enrich_edhrec_categories_from_scryfall: back-fill from Scryfall lookups
# ---------------------------------------------------------------------------

def _avg_deck(card_names: list[str]) -> AverageDeck:
    return AverageDeck(
        commander_name="X", slug="x",
        url="https://edhrec.com/average-decks/x",
        bracket_slug="upgraded", budget_slug=None,
        cards=[CardEntry(name=n, inclusion_pct=50.0) for n in card_names],
    )


def test_enrich_fills_categories_missing_from_edhrec_map():
    """Cards on the average deck but missing from the commander page's
    category_lists are categorized via Scryfall type_line."""
    avg = _avg_deck(["Birds of Paradise", "Sol Ring", "Path to Exile"])
    looked_up = {
        "birds of paradise": {"type_line": "Creature — Bird"},
        "sol ring": {"type_line": "Artifact"},
        "path to exile": {"type_line": "Instant"},
    }
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={},
        average_deck=avg,
        lookup_fn=lambda name: looked_up.get(name.lower()),
    )
    assert out["birds of paradise"] == "Creatures"
    assert out["sol ring"] == "Artifacts"
    assert out["path to exile"] == "Instants"


def test_enrich_does_not_overwrite_existing_edhrec_entries():
    """When EDHREC already bucketed Sol Ring as 'Mana Artifacts', the
    Scryfall fallback ('Artifacts') must not clobber the richer label."""
    avg = _avg_deck(["Sol Ring"])
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={"sol ring": "Mana Artifacts"},
        average_deck=avg,
        lookup_fn=lambda name: {"type_line": "Artifact"},
    )
    assert out["sol ring"] == "Mana Artifacts"


def test_enrich_skips_cards_scryfall_returns_none_for():
    """Custom cards / typos / Scryfall outage: lookup returns None.
    The card stays absent from the map and the UI surfaces 'Other'."""
    avg = _avg_deck(["Custom Made-Up Card"])
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={},
        average_deck=avg,
        lookup_fn=lambda name: None,
    )
    assert "custom made-up card" not in out


def test_enrich_skips_cards_with_unmapped_type_line():
    """Type lines outside the known set (Phenomenon, Plane, Tribal,
    etc.) return None from _category_from_type_line and shouldn't
    pollute the map with a fabricated label."""
    avg = _avg_deck(["Weird Card"])
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={},
        average_deck=avg,
        lookup_fn=lambda name: {"type_line": "Phenomenon"},
    )
    assert "weird card" not in out


def test_enrich_tolerates_lookup_raising():
    """Network blips on individual lookups must not poison the whole
    enrichment pass — the bad card is skipped, others continue."""
    avg = _avg_deck(["Lightning Bolt", "Broken Card", "Brainstorm"])

    def _flaky(name: str):
        if name.lower() == "broken card":
            raise RuntimeError("simulated transient")
        return {"type_line": "Instant"}

    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={}, average_deck=avg, lookup_fn=_flaky,
    )
    assert out["lightning bolt"] == "Instants"
    assert out["brainstorm"] == "Instants"
    assert "broken card" not in out


def test_enrich_uses_mdfc_front_face_type_line_when_top_level_empty():
    """Modal Double-Faced Cards ship with an empty top-level type_line
    on the projected snapshot; the front face's type_line is what
    matters for bucketing."""
    avg = _avg_deck(["Bala Ged Recovery"])
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={},
        average_deck=avg,
        lookup_fn=lambda name: {
            "type_line": "",
            "card_faces": [
                {"type_line": "Sorcery"},
                {"type_line": "Land"},
            ],
        },
    )
    assert out["bala ged recovery"] == "Sorceries"


def test_enrich_no_op_when_average_deck_is_none():
    """Defensive: advisor passes None when EDHREC didn't return an
    average deck; enrichment is a no-op."""
    base = {"sol ring": "Mana Artifacts"}
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories=base, average_deck=None,
        lookup_fn=lambda name: pytest.fail("should not be called"),
    )
    assert out == base


def test_enrich_no_op_when_average_deck_has_no_cards():
    """Defensive: empty card list — no lookups, original map returned."""
    avg = AverageDeck(
        commander_name="X", slug="x", url="https://edhrec.com/x",
        bracket_slug=None, budget_slug=None, cards=[],
    )
    out = _enrich_edhrec_categories_from_scryfall(
        edhrec_categories={"x": "Y"}, average_deck=avg,
        lookup_fn=lambda name: pytest.fail("should not be called"),
    )
    assert out == {"x": "Y"}
