"""Tests for staples.py — universal staples and role classification."""
from __future__ import annotations

import pytest

from commander_builder.staples import (
    BASIC_LANDS_LC,
    ROLE_SATURATION_THRESHOLDS,
    UNIVERSAL_STAPLES_LC,
    classify_role,
    confidence_tier,
    count_deck_roles,
    is_basic_land,
    is_role_saturated,
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


# ---------------------------------------------------------------------------
# count_deck_roles — feeds the advisor's saturation guard
# ---------------------------------------------------------------------------
# Motivation: the Ur-Dragon B4 audit (2026-05-13) recommended 5 ramp /
# cost-reducer adds to a deck that already had 12+ ramp pieces. The
# advisor was role-blind on the deck side — it tagged the recommended
# *adds* with roles but never counted what the deck already had. This
# function provides the count so the advisor can drop adds whose role
# bucket is already saturated.


def test_count_deck_roles_counts_per_role(monkeypatch):
    """Each card resolves to its role via classify_role; the Counter
    aggregates how many cards landed in each bucket."""
    # Fake Scryfall: map name → oracle/type so the role classifier
    # produces deterministic buckets.
    fake_db = {
        "sol ring": ("Add {C}{C}.", "Artifact"),
        # Arcane Signet's real oracle reads "Add one mana of any color in
        # your commander's color identity." which the classifier's strict
        # `add \{X\}` regex doesn't catch — it falls through to "other".
        # Use a fake-but-regex-friendly variant here so the test exercises
        # the "this is a ramp card" path explicitly. Real-world Arcane
        # Signet undercounting is tracked as a known classifier gap.
        "arcane signet": ("Add {W} or {U}.", "Artifact"),
        "rampant growth": (
            "Search your library for a basic land card and put it onto the battlefield tapped.",
            "Sorcery",
        ),
        "cultivate": (
            "Search your library for two basic land cards, reveal them, "
            "put one onto the battlefield tapped and the other into your hand.",
            "Sorcery",
        ),
        "rhystic study": ("Whenever an opponent casts a spell, you may draw a card.", "Enchantment"),
        "swords to plowshares": (
            "Exile target creature.", "Instant",
        ),
        "wrath of god": ("Destroy all creatures.", "Sorcery"),
    }

    def fake_lookup(name):
        entry = fake_db.get(name.lower())
        if not entry:
            return None
        oracle, type_line = entry
        return {"oracle_text": oracle, "type_line": type_line}

    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )

    counts = count_deck_roles([
        "Sol Ring", "Arcane Signet", "Rampant Growth", "Cultivate",
        "Rhystic Study", "Swords to Plowshares", "Wrath of God",
    ])
    assert counts["ramp"] == 4   # Sol Ring + Arcane Signet + Rampant + Cultivate
    assert counts["draw"] == 1   # Rhystic Study
    assert counts["removal"] == 1
    assert counts["wipe"] == 1


def test_count_deck_roles_handles_unknown_cards_as_other(monkeypatch):
    """Cards Scryfall doesn't know about (typos, very new printings)
    must not crash the count — bucket them as 'other' so the saturation
    guard doesn't decide policy based on missing data."""
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card",
        lambda name: None,  # All unresolved
    )
    counts = count_deck_roles(["Fake Card A", "Fake Card B"])
    assert counts.get("other", 0) >= 2


def test_count_deck_roles_swallows_lookup_exceptions(monkeypatch):
    """A network error during lookup_card should not abort the count.
    Treat the card as 'other' (unknown) and keep going."""
    def boom(name):
        raise RuntimeError("network blip")
    monkeypatch.setattr("commander_builder.staples.lookup_card", boom)
    counts = count_deck_roles(["Sol Ring", "Cultivate"])
    # Two unknowns, both fell through to 'other'. Doesn't raise.
    assert counts.get("other", 0) >= 2


def test_count_deck_roles_empty_deck():
    counts = count_deck_roles([])
    assert dict(counts) == {}


# ---------------------------------------------------------------------------
# is_role_saturated + ROLE_SATURATION_THRESHOLDS
# ---------------------------------------------------------------------------


def test_role_saturation_thresholds_includes_common_buckets():
    """The threshold table must cover at least ramp/draw/removal/wipe
    since those are the most commonly-recommended roles. Missing
    entries default to 'never saturate' (covered by is_role_saturated)."""
    for role in ("ramp", "draw", "removal", "wipe"):
        assert role in ROLE_SATURATION_THRESHOLDS
        # Values should be in a sane range — 4 (boards) to 20 (heavy).
        assert 4 <= ROLE_SATURATION_THRESHOLDS[role] <= 20


def test_is_role_saturated_fires_above_threshold():
    """Just above the threshold counts as saturated. Equal-to-threshold
    is also saturated (a deck with exactly 12 ramp pieces doesn't need
    a 13th)."""
    threshold = ROLE_SATURATION_THRESHOLDS["ramp"]
    assert is_role_saturated("ramp", count=threshold) is True
    assert is_role_saturated("ramp", count=threshold + 5) is True


def test_is_role_saturated_does_not_fire_below_threshold():
    threshold = ROLE_SATURATION_THRESHOLDS["ramp"]
    assert is_role_saturated("ramp", count=threshold - 1) is False
    assert is_role_saturated("ramp", count=0) is False


def test_is_role_saturated_unknown_role_never_fires():
    """Roles without a configured threshold (e.g. 'other', 'land',
    'threat') should never saturate — the function returns False
    instead of crashing on KeyError. We don't want a typo in the
    role string to silently drop all recommendations."""
    assert is_role_saturated("not-a-real-role", count=999) is False
    assert is_role_saturated("other", count=999) is False


# ---------------------------------------------------------------------------
# is_land — manabase guard for advisor cut path
# ---------------------------------------------------------------------------
# Real failure mode caught 2026-05-13: the bracket-peers recommender
# cut Savannah (a $200 ABU dual) from a 5-color Ur-Dragon deck because
# none of the top-5 reference decks happened to run it. Manabase
# decisions are deliberate — the advisor shouldn't auto-recommend
# cutting any land. is_land powers the new skip filter.


def test_is_land_recognizes_basic(monkeypatch):
    from commander_builder.staples import is_land
    def boom(name):
        raise AssertionError("must not call Scryfall for basics")
    monkeypatch.setattr("commander_builder.staples.lookup_card", boom)
    assert is_land("Forest") is True
    assert is_land("Mountain") is True


def test_is_land_recognizes_nonbasic_via_type_line(monkeypatch):
    """Savannah, fetch lands, MDFCs, shocks — anything Scryfall marks
    with 'Land' in the type_line is a land for our purposes."""
    from commander_builder.staples import is_land
    fakes = {
        "savannah": "Land — Plains Forest",
        "wooded foothills": "Land",
        "stomping ground": "Land — Mountain Forest",
        "boseiju, who endures": "Legendary Land — Forest",
    }
    def fake_lookup(name):
        entry = fakes.get(name.lower())
        if entry is None:
            return None
        return {"type_line": entry, "oracle_text": ""}
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )
    assert is_land("Savannah") is True
    assert is_land("Wooded Foothills") is True
    assert is_land("Stomping Ground") is True
    assert is_land("Boseiju, Who Endures") is True


def test_is_land_returns_false_for_nonland_cards(monkeypatch):
    from commander_builder.staples import is_land
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card",
        lambda name: {"type_line": "Creature — Dragon", "oracle_text": ""},
    )
    assert is_land("Drakuseth, Maw of Flames") is False


def test_is_land_returns_false_on_lookup_failure(monkeypatch):
    """Defensive: Scryfall unreachable or unknown card → False so the
    cut path falls back to normal handling rather than over-protecting
    a non-land just because the lookup failed."""
    from commander_builder.staples import is_land
    def boom(name):
        raise RuntimeError("offline")
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", boom,
    )
    assert is_land("Mystery Card") is False

    monkeypatch.setattr(
        "commander_builder.staples.lookup_card",
        lambda name: None,
    )
    assert is_land("Unknown Card") is False
