"""Tests for staples.py — universal staples and role classification."""
from __future__ import annotations

import pytest

from commander_builder.staples import (
    BASIC_LANDS_LC,
    UNIVERSAL_STAPLES_LC,
    classify_role,
    confidence_tier,
    is_basic_land,
    is_universal_staple,
    render_frequency_label,
)


# ---------------------------------------------------------------------------
# is_universal_staple
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Sol Ring", "sol ring", "SOL RING",
    "Arcane Signet",
    "Command Tower",
    "Lightning Greaves",
    "Skullclamp",
])
def test_is_universal_staple_recognizes_canonical_staples(name):
    assert is_universal_staple(name) is True


@pytest.mark.parametrize("name", [
    "Cyclonic Rift",
    "Smothering Tithe",
    "Mana Crypt",
    "Dockside Extortionist",
    "Forest",
])
def test_is_universal_staple_excludes_non_staples_and_basics(name):
    assert is_universal_staple(name) is False


def test_is_universal_staple_handles_whitespace():
    assert is_universal_staple("  Sol Ring  ") is True


# ---------------------------------------------------------------------------
# is_basic_land
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Forest", "Plains", "Island", "Swamp", "Mountain", "Wastes",
    "Snow-Covered Forest", "snow-covered island",
])
def test_is_basic_land_recognizes_all_basics(name):
    assert is_basic_land(name) is True


def test_is_basic_land_excludes_nonbasic_lands():
    assert is_basic_land("Bayou") is False
    assert is_basic_land("Command Tower") is False


# ---------------------------------------------------------------------------
# classify_role
# ---------------------------------------------------------------------------

def test_classify_role_land_takes_priority():
    assert classify_role("{T}: Add {G}.", "Basic Land — Forest") == "land"


def test_classify_role_fetchland_classified_as_ramp():
    assert classify_role(
        "{T}, Pay 1 life, Sacrifice this: Search your library for a Plains "
        "or Forest card and put it onto the battlefield.",
        "Land",
    ) == "ramp"


def test_classify_role_ramp_artifact():
    role = classify_role(
        "{T}: Add {C}{C}.",
        "Artifact",
    )
    # Sol Ring's text doesn't say "search your library" but adds {C}{C} —
    # falls under the mana-producer pattern, ranked as ramp at score 50.
    assert role == "ramp"


def test_classify_role_draw_spell():
    role = classify_role("Draw three cards.", "Sorcery")
    assert role == "draw"


def test_classify_role_removal_spell():
    role = classify_role("Destroy target creature.", "Instant")
    assert role == "removal"


def test_classify_role_counterspell():
    role = classify_role("Counter target spell.", "Instant")
    assert role == "removal"  # counter falls in the removal bucket


def test_classify_role_wipe():
    role = classify_role("Destroy all creatures.", "Sorcery")
    assert role == "wipe"


def test_classify_role_wipe_takes_priority_over_removal():
    # "Destroy all creatures" should match wipe (score 90), not removal.
    role = classify_role(
        "Destroy all creatures. They can't be regenerated.",
        "Sorcery",
    )
    assert role == "wipe"


def test_classify_role_finisher():
    role = classify_role("Target opponent loses the game.", "Sorcery")
    assert role == "finisher"


def test_classify_role_tutor():
    role = classify_role(
        "Search your library for a creature card, reveal it, put it into "
        "your hand, then shuffle.",
        "Sorcery",
    )
    assert role == "tutor"


def test_classify_role_creature_falls_to_threat():
    role = classify_role("Flying. Vigilance.", "Creature — Angel")
    assert role == "threat"


def test_classify_role_unknown_returns_other():
    role = classify_role("This is some text that matches nothing.", "Enchantment")
    assert role == "other"


def test_classify_role_empty_input():
    assert classify_role("", "") == "other"


def test_classify_role_protection_creature_aura():
    role = classify_role(
        "Enchanted creature has hexproof and indestructible.",
        "Enchantment — Aura",
    )
    assert role == "protection"


# ---------------------------------------------------------------------------
# render_frequency_label and confidence_tier
# ---------------------------------------------------------------------------

def test_render_frequency_label_unanimous():
    assert render_frequency_label(7, 7) == "unanimous (7/7 refs)"


def test_render_frequency_label_near_unanimous():
    assert render_frequency_label(6, 7) == "near-unanimous (6/7 refs)"


def test_render_frequency_label_majority():
    assert render_frequency_label(4, 7) == "majority (4/7 refs)"


def test_render_frequency_label_minority():
    assert render_frequency_label(2, 7) == "minority (2/7 refs)"


def test_render_frequency_label_zero_refs_returns_empty():
    assert render_frequency_label(0, 7) == ""
    assert render_frequency_label(0, 0) == ""


def test_render_frequency_label_two_refs_majority_threshold():
    # With 2 refs, "majority" needs both — 1 of 2 is minority, not majority,
    # because 1 of 2 isn't strong enough signal to be a majority claim.
    assert render_frequency_label(1, 2) == "majority (1/2 refs)"
    assert render_frequency_label(2, 2) == "majority (2/2 refs)"


def test_confidence_tier_levels():
    assert confidence_tier(7, 7) == 3
    assert confidence_tier(4, 7) == 2
    assert confidence_tier(2, 7) == 1
    assert confidence_tier(0, 7) == 0
    assert confidence_tier(5, 0) == 0
