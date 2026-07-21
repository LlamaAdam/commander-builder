"""Tests for staples.py — universal staples and role classification."""
from __future__ import annotations

import pytest

from commander_builder.staples import (
    BASIC_LANDS_LC,
    ROLE_SATURATION_THRESHOLDS,
    ROLE_TARGETS,
    UNIVERSAL_STAPLES_LC,
    classify_role,
    classify_role_extended,
    confidence_tier,
    count_deck_roles,
    is_basic_land,
    is_role_saturated,
    is_universal_staple,
    render_frequency_label,
)


# ---------------------------------------------------------------------------
# classify_role_extended — lands must win over land_payoff / win_condition
# ---------------------------------------------------------------------------

def test_classify_role_extended_land_with_landfall_text_is_land():
    # A land whose oracle text would match a land_payoff pattern must still
    # classify as a land (type line wins), not "land_payoff". Regression for
    # the missing type-line guard in classify_role_extended.
    role = classify_role_extended(
        "Whenever a land enters the battlefield under your control, "
        "create a 2/2 Zombie.",
        type_line="Land",
    )
    assert role == "land"


def test_classify_role_extended_nonland_payoff_still_classifies():
    # The land guard must not suppress land_payoff for actual non-land cards.
    role = classify_role_extended(
        "Landfall - whenever a land enters the battlefield under your "
        "control, draw a card.",
        type_line="Enchantment",
    )
    assert role == "land_payoff"


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


def test_classify_role_wipe_crux_of_fate_destroy_each_typed():
    # Real failure mode caught in the 2026-05-13 Ur-Dragon B4 chrome
    # test: Crux of Fate (a textbook wipe) was being classified as
    # ``other`` because the original pattern only matched the
    # "destroy all <type>" phrasing. Crux uses "destroy each ..."
    # with a typed clause. If this regresses, the dashboard's
    # categories panel reports wipe=0 for a deck that's clearly
    # running a wipe, and the saturation guard fires incorrectly.
    role = classify_role(
        "Choose one — Destroy each Dragon. Or — Destroy each non-Dragon "
        "creature.",
        "Sorcery",
    )
    assert role == "wipe"


def test_classify_role_wipe_destroy_each_creature():
    # Generic "destroy each creature" phrasing (e.g. Damnation flavored
    # variants). The "each" idiom is the modern-templating equivalent
    # of "all" and should classify the same way.
    role = classify_role("Destroy each creature.", "Sorcery")
    assert role == "wipe"


def test_classify_role_wipe_cyclonic_rift_overload_bounce():
    # Cyclonic Rift's overload mode: "Return each nonland permanent
    # you don't control to its owner's hand." Same category as
    # Evacuation / Devastation Tide — a board-wide bounce wipe.
    # Before the fix this matched the "return target ..." removal
    # pattern instead, classifying as removal.
    role = classify_role(
        "Return target nonland permanent you don't control to its owner's "
        "hand. Overload {1}{U}{U}{U} (You may cast this spell for its "
        "overload cost. If you do, change its text by replacing all "
        "instances of \"target\" with \"each.\")",
        "Instant",
    )
    assert role == "wipe"


def test_classify_role_wipe_evacuation_style_bounce():
    # Evacuation: "Return all creatures to their owners' hands."
    # This already passed via the "return all ... to ... owners'
    # hands" pattern; keeping the test guards against the broader
    # pattern rewrite below regressing it.
    role = classify_role(
        "Return all creatures to their owners' hands.",
        "Instant",
    )
    assert role == "wipe"


def test_classify_role_wipe_cyclonic_rift_real_scryfall_text():
    # See ``tests/fixtures/real_oracles.py`` for the byte-exact
    # Scryfall text and the bug history.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Cyclonic Rift")
    assert classify_role(o["oracle_text"], o["type_line"]) == "wipe"


def test_detect_themes_returns_token_theme_when_threshold_hit():
    """``detect_themes`` scans card oracles for archetype-indicator
    patterns and returns the EDHREC tag slugs of themes that
    clear their per-theme min-count threshold. Pinned for the
    Tokens theme (threshold = 8 cards with "create ... token" or
    similar phrasing).
    """
    from commander_builder.staples import detect_themes

    # 8 cards with token-creation text → Tokens slug should fire.
    deck = [
        (f"Token Maker {i}", "Create a 1/1 white Soldier creature token.")
        for i in range(8)
    ]
    deck += [("Filler", "Vanilla creature.")]
    themes = detect_themes(deck)
    assert "tokens" in themes


def test_detect_themes_skips_below_threshold():
    """Goodstuff decks with a few incidental theme cards should NOT
    trip a theme. The per-theme min-count threshold is the gate.
    """
    from commander_builder.staples import detect_themes

    # Only 3 token-making cards → below the 8-card threshold.
    deck = [
        ("T1", "Create a token."),
        ("T2", "Create a token."),
        ("T3", "Create a token."),
        ("Filler", "Vanilla creature."),
    ]
    themes = detect_themes(deck)
    assert "tokens" not in themes


def test_detect_themes_returns_multiple_themes_when_multiple_hit():
    """A deck that clears multiple theme thresholds gets all of
    them back (capped at 3, sorted by signal strength)."""
    from commander_builder.staples import detect_themes

    deck = (
        # 10 token-makers (clears Tokens threshold of 8)
        [(f"T{i}", "Create a 1/1 token.") for i in range(10)]
        # 8 sacrifice triggers (clears Aristocrats threshold of 8)
        + [(f"S{i}", "Whenever a creature you control dies, draw a card.")
           for i in range(8)]
    )
    themes = detect_themes(deck)
    # Both themes should fire; Tokens has more matches so it sorts first.
    assert "tokens" in themes
    assert "sacrifice" in themes
    assert themes[0] == "tokens"  # higher count wins ordering


def test_detect_themes_caps_at_3():
    """The result is capped at 3 slugs to bound the audit's
    cumulative HTTP cost (each tag-page fetch is 1-2s on cold
    cache).
    """
    from commander_builder.staples import detect_themes

    # 20 cards that each hit 4 different themes (token + sacrifice
    # + life-gain + counters).
    deck = [
        (
            f"C{i}",
            "Create a 1/1 token. Whenever a creature dies, "
            "you gain 1 life and put a +1/+1 counter on a creature.",
        )
        for i in range(20)
    ]
    themes = detect_themes(deck)
    assert len(themes) <= 3


def test_classify_role_wipe_crux_of_fate_real_scryfall_text():
    from tests.fixtures.real_oracles import oracle
    o = oracle("Crux of Fate")
    assert classify_role(o["oracle_text"], o["type_line"]) == "wipe"


def test_classify_role_ramp_basic_land_type_search():
    # Three Visits / Nature's Lore / Land Tax style — search for a
    # named basic land type rather than the generic word "land".
    from tests.fixtures.real_oracles import oracle
    o = oracle("Three Visits")
    assert classify_role(o["oracle_text"], o["type_line"]) == "ramp"


def test_classify_role_draw_additional_cards_idiom():
    # Sylvan Library / Howling Mine style — "draw two additional
    # cards" / "draw an additional card" templating.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Sylvan Library")
    assert classify_role(o["oracle_text"], o["type_line"]) == "draw"


def test_classify_role_wipe_minus_x_minus_x_mass_shrink():
    # Toxic Deluge / Crippling Fear style.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Toxic Deluge")
    assert classify_role(o["oracle_text"], o["type_line"]) == "wipe"


def test_classify_role_tutor_or_combined_types():
    # Mystical Tutor / Worldly Tutor / Eladamri's Call style.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Mystical Tutor")
    assert classify_role(o["oracle_text"], o["type_line"]) == "tutor"


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
        # Arcane Signet now classifies via its REAL oracle template
        # ("Add one mana of any color in your commander's color
        # identity") after the 2026-05-16 natural-language ramp regex
        # was added. Real text lives in tests/fixtures/real_oracles.py;
        # pinned here too so this test exercises the same path.
        "arcane signet": (
            "{T}: Add one mana of any color in your commander's color identity.",
            "Artifact",
        ),
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
    entries default to 'never saturate' (covered by is_role_saturated).

    Range 3..15 reflects the 2026-05-13 recalibration: post-1.1
    role classifier fixes mean counts are accurate, so thresholds
    align with EDH tuned-deck norms (ramp 10, wipe 4, finisher 3)
    rather than the padded-up values that compensated for under-
    counting bugs.
    """
    for role in ("ramp", "draw", "removal", "wipe"):
        assert role in ROLE_SATURATION_THRESHOLDS
        assert 3 <= ROLE_SATURATION_THRESHOLDS[role] <= 15


def test_role_saturation_thresholds_match_tuned_deck_norms():
    """Pin the recalibrated 2026-05-13 values so future drift surfaces
    in CI. These reflect what tuned EDH decks actually run:

      ramp: 8-10 standard, 12+ bloat
      draw: 8-10 standard, 12+ bloat (threshold raised 9 → 10 in
        2026-07 so the saturation ceiling can't sit below the
        ROLE_TARGETS floor — see the invariant test below)
      removal: 6-8 standard
      wipe: 2-4 standard
      protection: 3-5 standard
      tutor: 1-4 standard, heavier decks legitimately higher
      finisher: 1-2 specific 'lose the game' effects

    Bumping these requires a deliberate test update — exactly the
    friction we want so the role-saturation guard's behavior doesn't
    drift silently across releases.
    """
    assert ROLE_SATURATION_THRESHOLDS == {
        "ramp": 10,
        "draw": 10,
        "removal": 8,
        "wipe": 4,
        "protection": 5,
        "tutor": 5,
        "finisher": 3,
    }


def test_role_saturation_threshold_never_below_role_target():
    """INVARIANT: for every role with both a recommended-minimum
    (ROLE_TARGETS, the floor) and a saturation threshold
    (ROLE_SATURATION_THRESHOLDS, the ceiling), ceiling >= floor.

    If the ceiling ever dips below the floor there is a contradiction
    band of counts (threshold <= count < target) where the same audit
    says 'needs more X' (deficit > 0) while the redundancy guard
    refuses every X add (is_role_saturated is True). Exactly this
    happened with draw (threshold 9 < target 10) until 2026-07.
    """
    for role, target in ROLE_TARGETS.items():
        ceiling = ROLE_SATURATION_THRESHOLDS.get(role)
        if ceiling is None:
            # Roles without a threshold never saturate — no conflict.
            continue
        assert ceiling >= target, (
            f"role {role!r}: saturation threshold {ceiling} < target "
            f"{target} — the advisor would demand more {role} while "
            f"refusing every {role} add"
        )


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


# ---------------------------------------------------------------------------
# Manabase essentials — the canonical "your deck should have these" lands
# ---------------------------------------------------------------------------
# User feedback (2026-05-13): "tribal decks should have cavern of souls.
# All decks should have dual lands and bond lands and fetch lands."
# The advisor's heuristic + bracket_peers paths recommend lands when
# they happen to appear in references/EDHREC. This adds a deterministic
# safety net: regardless of source, surface missing color-identity-
# appropriate manabase essentials.


def test_essential_manabase_includes_all_abu_duals_for_five_color():
    """A 5-color (WUBRG) deck should be told about every ABU dual it
    doesn't already own — these are the canonical baseline manabase."""
    from commander_builder.staples import essential_manabase_for_colors
    essentials = essential_manabase_for_colors({"W", "U", "B", "R", "G"})
    expected_duals = {
        "Bayou", "Badlands", "Plateau", "Scrubland", "Savannah",
        "Taiga", "Tundra", "Tropical Island", "Underground Sea",
        "Volcanic Island",
    }
    assert expected_duals.issubset(set(essentials))


def test_essential_manabase_includes_fetches_matching_colors():
    """Fetch lands gate on whether their two target basic types lie
    inside the deck's color identity. A WG deck wants Windswept Heath
    (fetches Plains/Forest); it should NOT get Polluted Delta
    (fetches Island/Swamp — neither in identity)."""
    from commander_builder.staples import essential_manabase_for_colors
    wg = essential_manabase_for_colors({"W", "G"})
    assert "Windswept Heath" in wg          # plains/forest
    assert "Polluted Delta" not in wg       # island/swamp


def test_essential_manabase_includes_bond_lands_in_color_identity():
    """Bond lands ('untapped if an opponent has an untapped creature')
    are 2-color enemy + ally pairs. Surface only those that fit the
    deck's identity."""
    from commander_builder.staples import essential_manabase_for_colors
    # Bountiful Promenade is GW; expect it for any deck containing
    # both G and W.
    wubrg = essential_manabase_for_colors({"W", "U", "B", "R", "G"})
    assert "Bountiful Promenade" in wubrg


def test_essential_manabase_excludes_off_color_lands_for_monocolor():
    """Mono-red deck should not be recommended Bayou (BG)."""
    from commander_builder.staples import essential_manabase_for_colors
    mono_r = essential_manabase_for_colors({"R"})
    assert "Bayou" not in mono_r
    assert "Underground Sea" not in mono_r
    # Mono-color decks don't benefit from 2-color fetches as much,
    # so the function may legitimately return an empty list or only
    # the colorless utility lands (e.g., no duals fit a mono-R deck).
    # Pin only the negative assertion — we don't want a 1-color deck
    # being told "you need Bayou".


def test_essential_manabase_empty_for_colorless_identity():
    """Colorless commander (no W/U/B/R/G) → no color-gated lands.
    The function returns an empty list rather than crashing."""
    from commander_builder.staples import essential_manabase_for_colors
    assert essential_manabase_for_colors(set()) == []


def test_essential_manabase_uppercase_color_letters():
    """Color identity is conventionally upper-case WUBRG. Mixed-case
    input shouldn't matter — we normalize."""
    from commander_builder.staples import essential_manabase_for_colors
    upper = essential_manabase_for_colors({"W", "G"})
    lower = essential_manabase_for_colors({"w", "g"})
    assert upper == lower


# ---------------------------------------------------------------------------
# Tribal essentials — Cavern of Souls etc.
# ---------------------------------------------------------------------------


def test_detect_tribal_type_finds_dragon_in_ur_dragon_oracle():
    """The Ur-Dragon's oracle mentions 'Dragon' multiple times; the
    detector should return 'Dragon' so the tribal-essentials helper
    knows this is a Dragon-tribal commander."""
    from commander_builder.staples import detect_tribal_type
    oracle = (
        "Eminence — As long as The Ur-Dragon is in the command zone "
        "or on the battlefield, other Dragon spells you cast cost 1 "
        "less to cast. Flying. Whenever one or more Dragons you "
        "control attack, draw a card for each of those Dragons, then "
        "you may put a permanent card from your hand onto the "
        "battlefield."
    )
    assert detect_tribal_type(oracle, "Legendary Creature — Dragon Avatar") \
        == "Dragon"


def test_detect_tribal_type_returns_none_for_non_tribal_oracle():
    """A goodstuff commander with no creature-type mention returns None."""
    from commander_builder.staples import detect_tribal_type
    oracle = (
        "Whenever you draw a card, target opponent loses 1 life and "
        "you gain 1 life."
    )
    assert detect_tribal_type(oracle, "Legendary Creature — Human") is None


def test_detect_tribal_type_finds_goblin():
    """Krenko commander text should resolve to 'Goblin'."""
    from commander_builder.staples import detect_tribal_type
    oracle = "{T}: Create X 1/1 red Goblin creature tokens, where X is..."
    assert detect_tribal_type(oracle, "Legendary Creature — Goblin Warrior") \
        == "Goblin"


def test_detect_tribal_type_picks_most_frequent_when_multiple_match():
    """Frequency wins over canonical-list order when the oracle
    mentions multiple tribes. Synthetic example: oracle mentions
    'Spirit' twice and 'Dragon' once. Without a frequency tiebreaker,
    first-match returns Dragon (canonical-order-earlier). The
    frequency-aware detector returns Spirit instead — the tribe
    actually most-emphasized in the text."""
    from commander_builder.staples import detect_tribal_type
    oracle = (
        "Whenever a Spirit enters the battlefield under your control, "
        "you may pay {1}{W}. If you do, create a Spirit token. "
        "Whenever a Dragon you control attacks, draw a card."
    )
    assert detect_tribal_type(oracle, "Legendary Creature — Spirit") \
        == "Spirit"


def test_detect_tribal_type_uses_canonical_order_when_frequencies_tie():
    """When two tribes both appear exactly N times, fall back to the
    canonical-list order (more-played tribes first). Avoids
    nondeterminism on edge cases."""
    from commander_builder.staples import detect_tribal_type
    # Oracle mentions exactly one Dragon and one Goblin. Dragon is
    # earlier in _CANONICAL_TRIBAL_TYPES → Dragon wins.
    oracle = "Whenever a Dragon you control attacks, target Goblin gets +1/+1."
    assert detect_tribal_type(oracle, "Legendary Creature — Dragon") \
        == "Dragon"


def test_tribal_essential_lands_returns_cavern_and_path():
    """For any tribal commander, the essentials list should at minimum
    include Cavern of Souls (uncounterable) and Path of Ancestry
    (filter + scry for the tribe). Both are colorless mana costs so
    they fit any color identity."""
    from commander_builder.staples import tribal_essential_lands
    out = tribal_essential_lands("Dragon")
    assert "Cavern of Souls" in out
    assert "Path of Ancestry" in out


def test_tribal_essential_lands_empty_for_none():
    """Non-tribal commander → empty list (no extra recommendations)."""
    from commander_builder.staples import tribal_essential_lands
    assert tribal_essential_lands(None) == []


def test_tribal_essential_lands_includes_three_tree_city():
    """Three Tree City (taps for {C} or tribe-typed mana) belongs in
    every tribal deck regardless of color identity. Added 2026-05-16."""
    from commander_builder.staples import tribal_essential_lands
    out = tribal_essential_lands("Goblin")
    assert "Three Tree City" in out


def test_tribal_essential_lands_mono_color_includes_nykthos():
    """Mono-color tribal decks (e.g. Krenko Goblins, all-R) get
    Nykthos, Shrine to Nyx for devotion-scaling ramp. Multi-color
    tribal decks don't because rainbow devotion is dead."""
    from commander_builder.staples import tribal_essential_lands
    out = tribal_essential_lands("Goblin", color_identity={"R"})
    assert "Nykthos, Shrine to Nyx" in out


def test_tribal_essential_lands_multi_color_skips_nykthos():
    """Two-color tribal (e.g. Slivers WG, Dragons all-five) shouldn't
    surface Nykthos — devotion doesn't scale well when half the
    creatures contribute different pips."""
    from commander_builder.staples import tribal_essential_lands
    out_2c = tribal_essential_lands("Sliver", color_identity={"W", "G"})
    assert "Nykthos, Shrine to Nyx" not in out_2c
    out_5c = tribal_essential_lands(
        "Dragon", color_identity={"W", "U", "B", "R", "G"},
    )
    assert "Nykthos, Shrine to Nyx" not in out_5c


def test_tribal_essential_lands_default_no_color_identity_unchanged():
    """Legacy callers that don't pass color_identity still get the
    base tribal land set — Nykthos requires the mono-color signal."""
    from commander_builder.staples import tribal_essential_lands
    out = tribal_essential_lands("Goblin")
    assert "Nykthos, Shrine to Nyx" not in out


def test_tribal_essential_lands_orders_path_of_ancestry_last():
    """Path of Ancestry's filter mana is dead weight on a mono-color
    tribal deck; the up-front fixers (Secluded Courtyard / Unclaimed
    Territory / Three Tree City) should outrank it in the recommended
    order. Pinned so a future ordering change doesn't silently regress
    the user-reported priority issue from 2026-05-16."""
    from commander_builder.staples import tribal_essential_lands
    out = tribal_essential_lands("Goblin")
    idx_path = out.index("Path of Ancestry")
    for higher_priority in (
        "Cavern of Souls", "Three Tree City",
        "Secluded Courtyard", "Unclaimed Territory",
    ):
        assert out.index(higher_priority) < idx_path, (
            f"{higher_priority} must rank above Path of Ancestry"
        )


# ---------------------------------------------------------------------------
# Utility fixing lands — colorless-mana-cost any-color lands for 3+ color decks
# ---------------------------------------------------------------------------
# Open backlog item from the resume session: City of Brass / Mana
# Confluence / Reflecting Pool / Forbidden Orchard fix any color but
# only earn their slot in 3+ color decks. Mono and 2-color decks
# already have efficient duals; pain-fixers are slot inefficiency
# there.


def test_utility_fixing_lands_returns_canonical_set_for_three_color():
    """3-color deck (Bant, Naya, etc.) should be told about City of
    Brass + Mana Confluence + Reflecting Pool."""
    from commander_builder.staples import utility_fixing_lands
    out = utility_fixing_lands({"G", "W", "U"})
    assert "City of Brass" in out
    assert "Mana Confluence" in out
    assert "Reflecting Pool" in out


def test_utility_fixing_lands_empty_for_one_or_two_color_decks():
    """Mono-color and 2-color decks don't benefit enough from
    universal-fixers to justify the life loss / token gift."""
    from commander_builder.staples import utility_fixing_lands
    assert utility_fixing_lands({"R"}) == []
    assert utility_fixing_lands({"G", "W"}) == []


def test_utility_fixing_lands_for_five_color_full_set():
    """5-color deck wants every utility fixer."""
    from commander_builder.staples import utility_fixing_lands
    out = utility_fixing_lands({"W", "U", "B", "R", "G"})
    assert "City of Brass" in out
    assert "Mana Confluence" in out
    assert "Reflecting Pool" in out


def test_essential_manabase_includes_utility_fixers_for_three_color():
    """The main entry point surfaces utility fixers when applicable."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"G", "W", "U"})
    assert "City of Brass" in out
    assert "Mana Confluence" in out


def test_essential_manabase_budget_mode_excludes_abu_duals():
    """Budget mode strips the $200+ ABU duals (Bayou, etc.) — for users
    who explicitly opted out of the most expensive cards. Shock lands,
    bond lands, and utility fixers (all $30-and-under) stay."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"W", "U", "B", "R", "G"}, budget=True)
    # ABU duals stripped.
    assert "Bayou" not in out
    assert "Savannah" not in out
    assert "Volcanic Island" not in out
    # Shocks stay (Ravnica duals are affordable).
    assert "Stomping Ground" in out
    # Bond lands stay.
    assert "Bountiful Promenade" in out


def test_essential_manabase_budget_mode_excludes_fetches():
    """Onslaught + Zendikar fetches are also $25-60 each; budget mode
    drops them too. Shock-only manabase is the realistic budget path."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"W", "G"}, budget=True)
    assert "Windswept Heath" not in out


def test_essential_manabase_default_mode_unchanged():
    """budget=False (default) keeps all four tiers as before — the
    new flag is strictly additive."""
    from commander_builder.staples import essential_manabase_for_colors
    out_default = essential_manabase_for_colors({"W", "U", "B", "R", "G"})
    out_explicit_false = essential_manabase_for_colors(
        {"W", "U", "B", "R", "G"}, budget=False,
    )
    assert out_default == out_explicit_false
    assert "Bayou" in out_default
    assert "Windswept Heath" in out_default


def test_essential_manabase_excludes_utility_fixers_for_two_color():
    """2-color WG (Selesnya) wants dual + fetch + shock + bond, but
    NOT City of Brass (pain-fixer earns less than a Temple Garden)."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"W", "G"})
    assert "Savannah" in out          # 2-color duals stay
    assert "Temple Garden" in out     # shocks stay
    assert "City of Brass" not in out
