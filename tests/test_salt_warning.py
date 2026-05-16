"""Tests for the audit-panel salt-warning aggregator.

EDHREC's salt list ranks cards 0..5 by how often they cause social
friction at casual tables (the canonical "salty" picks: Smothering
Tithe, Cyclonic Rift, Armageddon, ...). The audit panel ALREADY
annotates individual adds/cuts with a per-rec salt score (commit
553187e); this helper produces the AGGREGATE used by the banner that
fires above the recommendations when the user's CURRENT deck carries
salty picks at a low bracket.

Three layers of behavior:

1. **Bracket gate.** At B4/B5, salty picks are expected — the
   banner would be noise. Return None unconditionally. The cut-off
   lives in ``_SALT_WARN_BRACKET_MAX``; default is 3.
2. **Threshold gate.** Cards score 0..5; we only flag ones at or
   above 1.5 (EDHREC's own "noticeable salt" line).
3. **Aggregation.** Walks the user's CURRENT deck text (not the
   recommendation list — we want the banner to fire even if the
   advisor didn't suggest cutting the salty card). Returns sorted
   by salt desc, then name asc for stable display.

Casing preservation: the salt-list is keyed lowercase but the .dck
preserves canonical casing. The banner reads the canonical form back
so users see "Smothering Tithe", not "smothering tithe".
"""
from __future__ import annotations

import pytest

from commander_builder.web._helpers import project_salt_warning


_DECK_TEXT_WITH_SALT = (
    "[metadata]\nName=Test\n[Commander]\n1 Atraxa, Praetors' Voice\n"
    "[Main]\n"
    "1 Smothering Tithe|CMM|123\n"
    "1 Cyclonic Rift\n"
    "1 Sol Ring|CLB|871\n"
    "1 Forest\n"
)


_DECK_TEXT_NO_SALT = (
    "[metadata]\nName=Test\n[Commander]\n1 Test Commander\n"
    "[Main]\n"
    "1 Sol Ring\n"
    "1 Forest\n"
    "1 Mountain\n"
)


_SALT_MAP = {
    "smothering tithe": 3.2,
    "cyclonic rift": 4.5,
    "armageddon": 4.8,
    "sol ring": 0.8,  # below threshold
}


# ---------------------------------------------------------------------------
# Bracket gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bracket", [1, 2, 3])
def test_warning_fires_at_brackets_1_2_3(bracket):
    """Casual + focused brackets (B1-B3) get the warning. Salt is
    socially relevant at those tables."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=bracket,
    )
    assert result is not None
    assert result["bracket"] == bracket
    assert result["count"] == 2  # Smothering Tithe + Cyclonic Rift


@pytest.mark.parametrize("bracket", [4, 5])
def test_warning_suppressed_at_brackets_4_5(bracket):
    """High-power tables expect salty picks. The banner would just
    be noise — suppress unconditionally."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=bracket,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Threshold gate
# ---------------------------------------------------------------------------

def test_only_cards_at_or_above_threshold_surface():
    """Cards with salt < threshold are quiet — Sol Ring sits at 0.8
    in our test map, which is below the 1.5 default cut-off. The
    banner must NOT list it."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=2,
    )
    names = {c["name"] for c in result["cards"]}
    assert "Sol Ring" not in names
    assert "Smothering Tithe" in names
    assert "Cyclonic Rift" in names


def test_custom_threshold_filters_more_aggressively():
    """A stricter threshold drops more cards. Pinned because the
    default should remain configurable for users who want a tighter
    casual table or for tests pinning specific behavior."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=2, threshold=4.0,
    )
    names = {c["name"] for c in result["cards"]}
    assert "Smothering Tithe" not in names  # 3.2 < 4.0
    assert "Cyclonic Rift" in names         # 4.5 >= 4.0


# ---------------------------------------------------------------------------
# None pass-through cases
# ---------------------------------------------------------------------------

def test_returns_none_when_salt_map_empty():
    """No salt-list data → no banner. The UI keeps the audit panel
    rendering normally; we don't want to imply 'no salt' when we
    actually have 'no data'."""
    result = project_salt_warning(_DECK_TEXT_WITH_SALT, {}, bracket=2)
    assert result is None


def test_returns_none_when_no_salty_cards_in_deck():
    """Clean deck at a low bracket → no banner. The advisor's per-
    rec salt annotations would also be empty in this case, so the
    headline is honest."""
    result = project_salt_warning(_DECK_TEXT_NO_SALT, _SALT_MAP, bracket=2)
    assert result is None


def test_returns_none_when_deck_text_empty():
    """Defensive: empty deck text → no banner. Never crash on the
    parse, never falsely claim cards present."""
    result = project_salt_warning("", _SALT_MAP, bracket=2)
    assert result is None


# ---------------------------------------------------------------------------
# Aggregation shape + ordering
# ---------------------------------------------------------------------------

def test_cards_sorted_by_salt_descending_then_name():
    """Banner ordering matters: highest-salt card up top so the user
    sees the worst offender first."""
    deck = (
        "[Main]\n"
        "1 Armageddon\n"
        "1 Cyclonic Rift\n"
        "1 Smothering Tithe\n"
    )
    result = project_salt_warning(deck, _SALT_MAP, bracket=2)
    names_in_order = [c["name"] for c in result["cards"]]
    assert names_in_order == ["Armageddon", "Cyclonic Rift", "Smothering Tithe"]


def test_canonical_casing_preserved_for_display():
    """The salt-list is keyed lowercase but the .dck format preserves
    casing. The banner reads back the canonical form so users see
    'Smothering Tithe' not 'smothering tithe'."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=2,
    )
    names = {c["name"] for c in result["cards"]}
    # .dck stored as 'Smothering Tithe|CMM|123' → canonical='Smothering Tithe'
    assert "Smothering Tithe" in names
    # No lowercase versions sneaking through.
    assert "smothering tithe" not in names


def test_salt_score_rounded_to_two_decimals():
    """Pin the rounding so the banner doesn't render 'salt 3.2000000004'.
    EDHREC's own UI uses one decimal — we use two for tooltip
    fidelity but stop there."""
    odd_map = {"smothering tithe": 3.234567}
    deck = "[Main]\n1 Smothering Tithe\n"
    result = project_salt_warning(deck, odd_map, bracket=2)
    assert result["cards"][0]["salt"] == 3.23


def test_handles_edition_codes_in_card_lines():
    """Real .dck lines look like '1 Smothering Tithe|CMM|123'. The
    helper must compare the name portion before the pipe, not the
    entire line."""
    deck = "[Main]\n1 Smothering Tithe|CMM|123\n"
    result = project_salt_warning(deck, _SALT_MAP, bracket=2)
    assert result is not None
    assert result["cards"][0]["name"] == "Smothering Tithe"


def test_handles_non_numeric_salt_values_gracefully():
    """If the salt-list cache somehow carries a non-float entry
    (corrupted cache, parser bug), skip the card instead of
    crashing the whole banner."""
    weird_map = {
        "smothering tithe": "not-a-number",
        "cyclonic rift": 4.5,
    }
    result = project_salt_warning(_DECK_TEXT_WITH_SALT, weird_map, bracket=2)
    names = {c["name"] for c in result["cards"]}
    # Smothering Tithe skipped silently; Cyclonic Rift survives.
    assert "Smothering Tithe" not in names
    assert "Cyclonic Rift" in names


def test_response_carries_threshold_used():
    """The response surfaces the threshold so the banner text can
    stay truthful even when we tune the default."""
    result = project_salt_warning(
        _DECK_TEXT_WITH_SALT, _SALT_MAP, bracket=2, threshold=2.5,
    )
    assert result["threshold"] == 2.5
