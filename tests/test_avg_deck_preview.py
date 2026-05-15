"""Tests for the EDHREC average-deck preview projection.

The advisor commit 4ee8a0e plumbs an ``AverageDeck`` and the
``edhrec_categories`` map through ``AdviceReport``. This module pins
the contract for ``project_average_deck_preview()``, the helper that
turns those raw fields into the JSON-friendly dict the audit-panel UI
consumes under the response key ``average_deck_preview``.

Three layers of behavior:

1. **None pass-through.** When the advisor couldn't reach EDHREC or
   the commander has no published average deck for this bracket, the
   helper returns ``None`` and the UI's ``<details>`` panel stays
   hidden. We MUST NOT fabricate an empty preview — silent zeros
   would mask a real failure.

2. **Per-card projection.** Each card surfaces ``name``,
   ``inclusion_pct``, ``category`` (from edhrec_categories, lowercase
   key match), and ``in_user_deck`` (case-insensitive presence in the
   user's current .dck text). The card list preserves the input
   order — EDHREC's ranking on the average deck page is meaningful.

3. **Categorization.** When ``edhrec_categories`` carries an entry
   for the card (case-folded match), the helper returns the canonical
   category string. Cards missing from the category map fall through
   to ``None`` rather than guessing.

Real-fixture stance: this module uses tiny hand-built ``AverageDeck``
and ``CardEntry`` objects rather than scraped HTML — the helper's
contract is "given these inputs, project this output", which is
properly unit-test territory. End-to-end EDHREC parsing is covered
in ``tests/test_edhrec_client.py``.
"""
from __future__ import annotations

import pytest

from commander_builder.edhrec_client import AverageDeck, CardEntry
from commander_builder.web._helpers import project_average_deck_preview


def _sample_avg_deck(cards: list[tuple[str, float]]) -> AverageDeck:
    """Build a minimal AverageDeck for the test inputs."""
    return AverageDeck(
        commander_name="Test Commander",
        slug="test-commander",
        url="https://edhrec.com/average-decks/test-commander/upgraded",
        bracket_slug="upgraded",
        budget_slug=None,
        cards=[
            CardEntry(name=name, inclusion_pct=pct)
            for name, pct in cards
        ],
    )


_USER_DECK_TEXT = (
    "[metadata]\nName=Test\n[Commander]\n1 Test Commander\n"
    "[Main]\n"
    "1 Sol Ring|CLB|871\n"
    "1 Arcane Signet\n"
    "1 Forest\n"
)


# ---------------------------------------------------------------------------
# None pass-through
# ---------------------------------------------------------------------------

def test_returns_none_when_average_deck_is_none():
    """Advisor produced no AverageDeck → no preview surfaces. The UI
    panel relies on this to know whether to render the <details>."""
    result = project_average_deck_preview(
        average_deck=None,
        edhrec_categories={"sol ring": "Mana Artifacts"},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result is None


def test_returns_none_when_average_deck_has_no_cards():
    """An empty AverageDeck (EDHREC returned the page but parsed zero
    cards) is treated as 'no preview' rather than 'preview with 0
    cards' — empty <details> sections are misleading UX."""
    avg = _sample_avg_deck([])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={}, user_deck_text=_USER_DECK_TEXT,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Happy path — full projection
# ---------------------------------------------------------------------------

def test_projects_card_list_with_inclusion_pct_preserved():
    """Each card lands in the output preserving its name + inclusion%.
    The list ordering mirrors AverageDeck.cards — EDHREC ranks the
    average deck page by typical-build prominence, and we don't
    re-sort."""
    avg = _sample_avg_deck([
        ("Sol Ring", 95.5),
        ("Arcane Signet", 88.0),
        ("Cultivate", 72.0),
    ])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result is not None
    assert result["card_count"] == 3
    assert result["bracket_slug"] == "upgraded"
    assert [c["name"] for c in result["cards"]] == [
        "Sol Ring", "Arcane Signet", "Cultivate",
    ]
    assert result["cards"][0]["inclusion_pct"] == 95.5


def test_in_user_deck_flag_is_case_insensitive():
    """The .dck format mixes casing across sets; presence detection
    must fold case. 'Sol Ring' in the average deck matches the user's
    '1 Sol Ring' line; 'cultivate' (lowercased on the way in) still
    matches the user's '1 Cultivate' line."""
    avg = _sample_avg_deck([
        ("Sol Ring", 95.0),       # in user deck
        ("Lightning Bolt", 40.0),  # NOT in user deck
        ("Arcane Signet", 88.0),  # in user deck
        ("Forest", 60.0),         # in user deck
    ])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={},
        user_deck_text=_USER_DECK_TEXT,
    )
    flags = {c["name"]: c["in_user_deck"] for c in result["cards"]}
    assert flags["Sol Ring"] is True
    assert flags["Lightning Bolt"] is False
    assert flags["Arcane Signet"] is True
    assert flags["Forest"] is True


def test_in_user_deck_handles_edition_codes():
    """Real .dck lines look like '1 Sol Ring|CLB|871'. The presence
    check must compare card name only (before the pipe), not the
    entire line. _USER_DECK_TEXT carries both 'Sol Ring|CLB|871' and
    'Arcane Signet' (no edition) — both must match."""
    avg = _sample_avg_deck([("Sol Ring", 95.0)])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result["cards"][0]["in_user_deck"] is True


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------

def test_category_assigned_when_edhrec_categories_has_card():
    """edhrec_categories is the lowercase-name → section-header map
    sourced from the commander page's category_lists. When a card in
    the average deck appears in that map, surface the canonical
    category string."""
    avg = _sample_avg_deck([
        ("Sol Ring", 95.0),
        ("Cultivate", 72.0),
    ])
    categories = {
        "sol ring": "Mana Artifacts",
        "cultivate": "Ramp",
    }
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories=categories,
        user_deck_text=_USER_DECK_TEXT,
    )
    cat_by_name = {c["name"]: c["category"] for c in result["cards"]}
    assert cat_by_name["Sol Ring"] == "Mana Artifacts"
    assert cat_by_name["Cultivate"] == "Ramp"


def test_category_falls_back_to_none_when_card_missing_from_map():
    """The category map is built from the commander-page category_lists
    which don't always cover every average-deck entry. Missing cards
    get category=None — the UI groups those under an 'Other' bucket
    rather than the helper inventing a label."""
    avg = _sample_avg_deck([("Mystery Card", 40.0)])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={"sol ring": "Mana Artifacts"},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result["cards"][0]["category"] is None


def test_category_match_is_case_insensitive():
    """edhrec_categories is keyed by lowercase name, but the average
    deck preserves EDHREC's casing. The match must fold case both
    ways so 'CULTIVATE' in the avg deck still finds 'cultivate' in
    the map."""
    avg = _sample_avg_deck([("CULTIVATE", 72.0)])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={"cultivate": "Ramp"},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result["cards"][0]["category"] == "Ramp"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_user_deck_text_marks_everything_as_not_in_deck():
    """If the user deck text is empty (shouldn't happen in practice
    but defensive), every card in the average deck is flagged as
    not-in-deck — never crash, never falsely claim presence."""
    avg = _sample_avg_deck([("Sol Ring", 95.0)])
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={},
        user_deck_text="",
    )
    assert result["cards"][0]["in_user_deck"] is False


def test_bracket_slug_preserved_when_none():
    """Some EDHREC pages don't carry a bracket slug (older /average-
    decks/<slug> URLs that predate the bracket split). Pass the
    None through so the UI can render 'Average deck (any bracket)'
    rather than inventing a value."""
    avg = AverageDeck(
        commander_name="X", slug="x",
        url="https://edhrec.com/average-decks/x",
        bracket_slug=None, budget_slug=None,
        cards=[CardEntry(name="Sol Ring", inclusion_pct=95.0)],
    )
    result = project_average_deck_preview(
        average_deck=avg, edhrec_categories={},
        user_deck_text=_USER_DECK_TEXT,
    )
    assert result["bracket_slug"] is None
