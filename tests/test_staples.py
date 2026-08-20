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


# ---------------------------------------------------------------------------
# Round-2 evergreen gaps (2026-08-16) — six confirmed classify_role misses
# (all returned "other" against real Scryfall text) plus the treasure-plural
# ramp gap. Every fixture below comes from tests/fixtures/real_oracles.py
# per the real-oracle discipline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Negate",           # counter target noncreature spell
    "Dovin's Veto",     # can't-be-countered rider + noncreature counter
    "Spell Pierce",     # noncreature + unless-controller-pays form
    "Swan Song",        # enchantment, instant, or sorcery type list
])
def test_classify_role_removal_restricted_counterspells(name):
    # The original pattern required "spell" immediately after
    # "target", so every restricted counterspell fell to "other".
    from tests.fixtures.real_oracles import oracle
    o = oracle(name)
    assert classify_role(o["oracle_text"], o["type_line"]) == "removal"


@pytest.mark.parametrize("name", [
    "Light Up the Stage",   # plural, "until the end of your next turn"
    "Wrenn's Resolve",      # bare two-card impulse template
])
def test_classify_role_draw_impulse_exile_to_play(name):
    from tests.fixtures.real_oracles import oracle
    o = oracle(name)
    assert classify_role(o["oracle_text"], o["type_line"]) == "draw"


def test_classify_role_draw_impulse_engine_prosper():
    # Singular form ("exile the top card ... you may play that
    # card") on a creature; impulse draw (60) must beat both the
    # threat fallback and Prosper's treasure-ramp clause (40).
    from tests.fixtures.real_oracles import oracle
    o = oracle("Prosper, Tome-Bound")
    assert classify_role(o["oracle_text"], o["type_line"]) == "draw"


def test_classify_role_impulse_guard_cascade_reminder_not_draw():
    # Cascade's reminder text ("exile cards from the top of your
    # library ... You may cast it without paying its mana cost")
    # must NOT trip the impulse-draw pattern — Bloodbraid Elf is a
    # threat, not a draw spell.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Bloodbraid Elf")
    assert classify_role(o["oracle_text"], o["type_line"]) == "threat"


def test_classify_role_removal_fight_prey_upon():
    from tests.fixtures.real_oracles import oracle
    o = oracle("Prey Upon")
    assert classify_role(o["oracle_text"], o["type_line"]) == "removal"


def test_classify_role_removal_bite_ram_through():
    # One-sided fight: "deals damage equal to its power to target
    # creature" with no "fights" keyword anywhere.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Ram Through")
    assert classify_role(o["oracle_text"], o["type_line"]) == "removal"


@pytest.mark.parametrize("name", [
    "Diabolic Edict",   # target player sacrifices a creature
    "Soul Shatter",     # each opponent sacrifices a creature or planeswalker
])
def test_classify_role_removal_edicts(name):
    from tests.fixtures.real_oracles import oracle
    o = oracle(name)
    assert classify_role(o["oracle_text"], o["type_line"]) == "removal"


def test_classify_role_edict_guard_own_sacrifice_cost_not_removal():
    # Sacrificing YOUR OWN creature as an activation cost (Ashnod's
    # Altar) must never read as edict removal — the edict pattern is
    # anchored on "(each|target) (opponent|player) sacrifices".
    from tests.fixtures.real_oracles import oracle
    o = oracle("Ashnod's Altar")
    role = classify_role(o["oracle_text"], o["type_line"])
    assert role != "removal"
    assert role == "ramp"   # via its "Add {C}{C}" clause


@pytest.mark.parametrize("name", [
    "Earthquake",       # deals X damage to each creature ... and each player
    "Chain Reaction",   # deals X damage to each creature, where X is ...
])
def test_classify_role_wipe_x_damage_each_creature(name):
    # The original wipe pattern required literal digits, so every
    # X-damage sweep classified "other".
    from tests.fixtures.real_oracles import oracle
    o = oracle(name)
    assert classify_role(o["oracle_text"], o["type_line"]) == "wipe"


def test_classify_role_wipe_damage_equal_to_each_creature():
    # Widespread Brutality: "deals damage equal to its power to each
    # non-Army creature" — no digits, no literal X.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Widespread Brutality")
    assert classify_role(o["oracle_text"], o["type_line"]) == "wipe"


@pytest.mark.parametrize("name", [
    "Miirym, Sentinel Wyrm",      # ward {2} in a keyword line
    "Phyrexian Fleshgorger",      # Ward—Pay ... em-dash cost form
])
def test_classify_role_intrinsic_ward_is_not_a_protection_slot(name):
    """A creature that merely HAS ward is a resilient threat, not a
    protection card. The ``protection`` role feeds a ROLE_TARGETS quota
    meant to guarantee a deck can protect its commander — filling it
    with ward-carrying bodies would suppress the advisor's real
    protection recommendations. Only GRANTED ward counts (see
    ``test_classify_role_protection_granted_ward``)."""
    from tests.fixtures.real_oracles import oracle
    o = oracle(name)
    assert classify_role(o["oracle_text"], o["type_line"]) != "protection"


@pytest.mark.parametrize("text", [
    # Equipment / Aura phrasings.
    "Equipped creature gets +1/+1 and has ward {2}.",
    "Enchanted creature has ward—Pay 3 life.",
    # Instant / static grants, singular and plural subjects.
    "Target creature you control gains ward {1} until end of turn.",
    "Creatures you control have ward {1}.",
])
def test_classify_role_protection_granted_ward(text):
    """Synthetic POSITIVE guard for the grant phrasings. Kept synthetic
    deliberately: these are template shapes, not one card's text, and
    the real-oracle fixture discipline covers the negative cases above
    with verbatim Scryfall data."""
    assert classify_role(text, "Artifact — Equipment") == "protection"


def test_classify_role_ward_guard_requires_cost_marker():
    # The ward pattern demands "{" or the em-dash right after the
    # keyword, so a card-name mention ("Ward of Bones") or words
    # containing "ward" never classify as protection. Synthetic
    # NEGATIVE guard — no real card needs to exist for the
    # non-match to be worth pinning.
    assert classify_role(
        "Sacrifice Ward of Bones: each opponent discards a card.",
        "Artifact",
    ) != "protection"
    assert classify_role(
        "Creatures you control can attack as though they didn't have "
        "defender. Move toward victory as you reap your reward.",
        "Enchantment",
    ) != "protection"


def test_classify_role_protection_phasing_real_oracle():
    """Teferi's Protection classifies protection with phasing in the
    table (round-2 review 2026-08-20, R2-P11)."""
    from tests.fixtures.real_oracles import oracle
    o = oracle("Teferi's Protection")
    assert classify_role(o["oracle_text"], o["type_line"]) == "protection"


def test_phasing_pattern_fires_on_real_phase_out_text():
    """The phasing PATTERN itself matches Teferi's Protection, not just
    the card's classification.

    Needed because Teferi's Protection also carries "protection from
    everything", so the classification test above would pass even if
    the phasing pattern were deleted — this asserts the new pattern is
    what does the work for phase-out text. Reads the pattern out of the
    public ``_ROLE_PATTERNS`` table (those strings are the contract —
    ``interaction.py`` imports them too) rather than re-typing the
    regex, so a rewrite can't leave this test silently passing.
    """
    import re

    from commander_builder.staples import _ROLE_PATTERNS
    from tests.fixtures.real_oracles import oracle

    protection = dict(_ROLE_PATTERNS)["protection"]
    phasing = [p for p, _t, _s in protection if "phase" in p]
    assert phasing, (
        "no phasing pattern in the protection role table — R2-P11 "
        "regressed"
    )
    text = oracle("Teferi's Protection")["oracle_text"].lower()
    assert any(re.search(p, text) for p in phasing)


def test_classify_role_protection_granted_shield_counter_real_oracle():
    """Take Up the Shield's only protection signal is the shield
    counter it grants — its +2/+2 and lifelink riders match nothing
    else in the role table, so this is an isolated pin for the new
    pattern."""
    from tests.fixtures.real_oracles import oracle
    o = oracle("Take Up the Shield")
    assert classify_role(o["oracle_text"], o["type_line"]) == "protection"


@pytest.mark.parametrize("text,type_line", [
    # Synthetic NEGATIVE guards, same reasoning as intrinsic ward: a
    # permanent that arrives with its OWN shield counter protects
    # nothing but itself and must not fill the protection quota. The
    # "enters with" templating carries no "put ... on", which is what
    # the pattern keys on.
    ("Flying\nThis creature enters with two shield counters on it.",
     "Creature — Angel Soldier"),
    ("This creature enters with a shield counter on it.",
     "Creature — Soldier"),
])
def test_classify_role_intrinsic_shield_counter_is_not_protection(
        text, type_line):
    assert classify_role(text, type_line) != "protection"


def test_classify_role_ramp_treasure_plural_dockside():
    # "create X Treasure tokens" — the singular "create a treasure
    # token" pattern missed every plural/variable Treasure producer.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Dockside Extortionist")
    assert classify_role(o["oracle_text"], o["type_line"]) == "ramp"


def test_classify_role_big_score_draw_clause_still_wins():
    # Big Score creates two Treasures AND draws two cards; the draw
    # role (70) must keep outranking the new treasure-ramp match
    # (40) — the round-2 fix adds ramp-pattern coverage without
    # reclassifying draw spells.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Big Score")
    assert classify_role(o["oracle_text"], o["type_line"]) == "draw"


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


# ---------------------------------------------------------------------------
# Triomes + surveil duals (2026-08 manabase modernization)
# ---------------------------------------------------------------------------


def test_essential_manabase_includes_triomes_for_three_color():
    """A 3-color identity picks up exactly the triomes whose three
    colors all sit inside the identity."""
    from commander_builder.staples import essential_manabase_for_colors
    abzan = essential_manabase_for_colors({"W", "B", "G"})
    assert "Indatha Triome" in abzan            # WBG — exact match
    assert "Ketria Triome" not in abzan         # GUR — off-color


def test_essential_manabase_excludes_triomes_for_two_color():
    """Triomes need all THREE of their colors in the identity, so a
    2-color deck never sees one (the containment check gates them)."""
    from commander_builder.staples import essential_manabase_for_colors
    wg = essential_manabase_for_colors({"W", "G"})
    assert not any("Triome" in name for name in wg)
    assert "Jetmir's Garden" not in wg


def test_essential_manabase_five_color_gets_all_ten_triomes():
    from commander_builder.staples import essential_manabase_for_colors
    out = set(essential_manabase_for_colors({"W", "U", "B", "R", "G"}))
    expected = {
        "Indatha Triome", "Ketria Triome", "Raugrin Triome",
        "Savai Triome", "Zagoth Triome", "Jetmir's Garden",
        "Raffine's Tower", "Spara's Headquarters", "Xander's Lounge",
        "Ziatora's Proving Ground",
    }
    assert expected <= out


def test_essential_manabase_includes_surveil_duals_for_two_color():
    """MKM surveil duals are the top budget-tier default for any
    two-color pair — basic-typed (fetchable) + surveil on entry."""
    from commander_builder.staples import essential_manabase_for_colors
    dimir = essential_manabase_for_colors({"U", "B"})
    assert "Undercity Sewers" in dimir
    assert "Meticulous Archive" not in dimir    # WU — off-color
    mono_u = essential_manabase_for_colors({"U"})
    assert "Undercity Sewers" not in mono_u


def test_essential_manabase_budget_mode_keeps_triomes_and_surveil():
    """Budget mode strips ABU duals + fetches but keeps the cheap
    modern fixing: triomes ($3-15) and surveil duals ($2-8)."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"W", "U", "B", "R", "G"}, budget=True)
    assert "Bayou" not in out
    assert "Windswept Heath" not in out
    assert "Ketria Triome" in out
    assert "Undercity Sewers" in out


def test_essential_manabase_tier_order_untapped_before_tapped():
    """Tier order: untapped duals (ABU/fetch/shock/bond) outrank the
    tapped triomes, which outrank the tapped surveil duals; utility
    fixers come last. Pin with a Bant (GWU) identity where every tier
    has a representative."""
    from commander_builder.staples import essential_manabase_for_colors
    out = essential_manabase_for_colors({"G", "W", "U"})
    assert out.index("Temple Garden") < out.index("Spara's Headquarters")
    assert out.index("Sea of Clouds") < out.index("Spara's Headquarters")
    assert out.index("Spara's Headquarters") < out.index("Meticulous Archive")
    assert out.index("Meticulous Archive") < out.index("City of Brass")


# ---------------------------------------------------------------------------
# Politics detection (decision C2) — is_politics_card / politics_tags
# ---------------------------------------------------------------------------
#
# ORACLE-TEXT PROVENANCE, read this before adding a case. The repo's rule
# (tests/fixtures/real_oracles.py) is that classifier tests source oracle
# text verbatim from Scryfall, never from a hand-written approximation.
# No politics card is in that fixture yet and Scryfall is unreachable from
# the sandbox this landed in, so:
#
#   - Every NEGATIVE (false-positive) guard below uses a REAL fixture card
#     — those are the cases where an approximation would hide a bug, since
#     a false positive is by definition text nobody expected to match.
#   - Every POSITIVE case uses SYNTHETIC text, marked ``# SYNTHETIC`` and
#     written to the printed rules TEMPLATE the pattern targets, not to a
#     specific card. They pin pattern SHAPE only.
#
# FOLLOW-UP for a session with network: add Rhystic Study, Palace
# Sentinels, Marchesa's Decree, Council's Judgment, Tempt with Discovery,
# Propaganda and Disrupt Decorum to real_oracles.py (plus their entries in
# test_real_oracle_fixture.EXPECTED_ROLE) and re-point the positives here.
#
# 2026-08-20 (R2-P10): the first politics card DID land in the fixture —
# Smothering Tithe, whose punisher-tax template the guard was missing.
# Network was still blocked, so its body is an OFFLINE TRANSCRIPTION
# marked as such in the fixture module; re-verify it with the rest of the
# follow-up list. Its negative twin (Dance of the Dead's "If the player
# does" branch) is verbatim Scryfall text that was already in the fixture.

from commander_builder.staples import (  # noqa: E402
    POLITICS_SHIELD_REASON,
    is_politics_card,
    is_politics_card_name,
    politics_guard_enabled,
    politics_tags,
    politics_tags_for_name,
)
from tests.fixtures.real_oracles import oracle  # noqa: E402


@pytest.mark.parametrize("text,expected_tag", [
    # SYNTHETIC — goad keyword + its reminder text.
    ("Goad target creature. (Until your next turn, that creature attacks "
     "in combat if able and attacks a player other than you if able.)",
     "goad"),
    # SYNTHETIC — plural/third-person inflection ("goads each creature").
    ("At the beginning of combat on your turn, this creature goads each "
     "creature your opponents control.", "goad"),
    # SYNTHETIC — monarch reminder text, which every monarch card carries.
    ("When this creature enters, you become the monarch. (At the beginning "
     "of the monarch's end step, that player draws a card. Whenever a "
     "creature deals combat damage to the monarch, its controller becomes "
     "the monarch.)", "monarch"),
    # SYNTHETIC — the monarch hate side.
    ("Players can't become the monarch.", "monarch"),
    # SYNTHETIC — will of the council.
    ("Will of the council — Starting with you, each player votes for an "
     "artifact, creature, or enchantment.", "vote"),
    # SYNTHETIC — council's dilemma.
    ("Council's dilemma — Starting with you, each player votes for "
     "carnage or homage.", "vote"),
    # SYNTHETIC — the bare vote verb with no named mechanic.
    ("Each player may vote for an opponent.", "vote"),
    # SYNTHETIC — tempting offer.
    ("Tempting offer — Search your library for a land card. Each opponent "
     "may search their library for a land card.", "tempting_offer"),
    # SYNTHETIC — Rhystic-style tax (Rhystic Study / Mystic Remora shape).
    ("Whenever an opponent casts a spell, you may draw a card unless that "
     "player pays {1}.", "tax"),
    # SYNTHETIC — pillow-fort attack tax (Propaganda / Ghostly Prison).
    ("Creatures can't attack you unless their controller pays {2} for "
     "each creature they control that's attacking you.", "deterrent"),
    # SYNTHETIC — the same tax with a planeswalker rider between "you"
    # and "unless" (Norn's Annex shape); the bounded window must span it.
    ("Creatures can't attack you or planeswalkers you control unless "
     "their controller pays {W/P} for each of those creatures.",
     "deterrent"),
])
def test_politics_positive_shapes(text, expected_tag):
    """Each printed politics template is detected and tagged."""
    assert is_politics_card(text) is True
    assert expected_tag in politics_tags(text)


def test_politics_tax_punisher_template_real_smothering_tithe():
    """The flagship tax card, on REAL oracle text (R2-P10, 2026-08-20).

    Smothering Tithe's offer and consequence are two sentences with no
    "unless" ("that player may pay {2}. If the player doesn't, ...") —
    the original pattern returned no tags for the card the guard's own
    comment named as covered. This is the one positive politics case
    that is NOT synthetic; see the provenance note in
    tests/fixtures/real_oracles.py for how the text was sourced with
    Scryfall unreachable.
    """
    data = oracle("Smothering Tithe")
    assert is_politics_card(data["oracle_text"], data["type_line"]) is True
    assert "tax" in politics_tags(data["oracle_text"])


def test_politics_tax_punisher_positive_branch_is_not_a_tax():
    """"If the player DOES" is an optional cost, not a punisher tax.

    Dance of the Dead's upkeep line ("that player may pay {1}{B}. If
    the player does, untap that creature") has the same opening clause
    as Smothering Tithe but rewards paying instead of punishing not
    paying — nobody is being taxed, so the card must stay cuttable.
    Real fixture text, because a false positive is by definition text
    nobody expected to match.
    """
    data = oracle("Dance of the Dead")
    assert "that player may pay" in data["oracle_text"]
    assert is_politics_card(data["oracle_text"], data["type_line"]) is False


def test_politics_tax_punisher_sibling_subjects():
    """The subject alternation covers the each-opponent phrasing.

    SYNTHETIC — written to the printed template (Protection
    Racket-shaped upkeep punishers), not to one card's text, per the
    provenance rule at the top of this section.
    """
    body = ("At the beginning of your upkeep, each opponent may pay 3 "
            "life. If they don't, you draw a card.")
    assert "tax" in politics_tags(body)


@pytest.mark.parametrize("card_name", [
    # "unless its CONTROLLER pays" — a soft counterspell, not a Rhystic
    # tax. The AI plays this as ordinary interaction, so shielding it
    # would exempt a whole family of removal from every cut path.
    "Spell Pierce",
    # Ward is an "unless ... pays" cost too, in the em-dash form.
    "Phyrexian Fleshgorger",
    # Gives an opponent a token — table-facing, but no politics mechanic.
    "Swan Song",
    # "Target player sacrifices" — an opponent makes a choice, which is
    # NOT what politics means here (no negotiation, no vote, no tax).
    "Diabolic Edict",
    # Each-opponent effect with a choice, same reasoning.
    "Soul Shatter",
    # Broad control staples that must stay cuttable.
    "Wrath of God",
    "Cyclonic Rift",
    "Sylvan Library",
    "Arcane Signet",
])
def test_politics_negative_real_oracles(card_name):
    """Real Scryfall text that must NOT read as politics."""
    data = oracle(card_name)
    assert is_politics_card(data["oracle_text"], data["type_line"]) is False
    assert politics_tags(data["oracle_text"]) == ()


@pytest.mark.parametrize("text", [
    # SYNTHETIC word-boundary guards. "vote" inside a longer word is the
    # exact false positive the leading \b exists for.
    "As long as you have devotion to black, this creature gets +1/+1.",
    "Devoted Druid enters the battlefield tapped.",
    # Your OWN pay cost — cumulative upkeep / Braid of Fire shape. The
    # tax pattern requires "that player", i.e. an opponent.
    "At the beginning of your upkeep, sacrifice this unless you pay {2}.",
    # A creature that can't attack — no "unless" clause, so the pillow-
    # fort pattern must not latch onto the bare "can't attack you".
    "Creatures with power 2 or less can't attack you.",
    # Two unrelated sentences: "can't attack you." then an "unless" in
    # the NEXT sentence. The [^.] window must refuse to cross the stop.
    ("Creatures can't attack you. Sacrifice this enchantment unless you "
     "pay {1} during your upkeep."),
])
def test_politics_false_positive_guards(text):
    """SYNTHETIC near-miss templates that must stay unshielded."""
    assert is_politics_card(text) is False


def test_politics_tags_are_deduplicated_and_ordered():
    """A card matching two monarch patterns reports ``monarch`` once, and
    multi-mechanic cards report in table order (goad before monarch)."""
    # SYNTHETIC — a card that both goads and hands out the monarchy.
    text = ("Goad each creature your opponents control. You become the "
            "monarch. Players can't become the monarch this turn.")
    assert politics_tags(text) == ("goad", "monarch")


def test_politics_empty_text_is_not_politics():
    assert is_politics_card("") is False
    assert politics_tags("", "Artifact") == ()


# --- name-keyed wrapper ----------------------------------------------------

def test_politics_tags_for_name_uses_injected_lookup():
    """The injectable lookup keeps the predicate offline."""
    # SYNTHETIC oracle body; the point of the test is the seam.
    def lookup(name):
        return {"oracle_text": "Goad target creature.", "type_line": "Instant"}
    assert politics_tags_for_name("Whatever", lookup) == ("goad",)
    assert is_politics_card_name("Whatever", lookup) is True


def test_politics_name_unresolvable_is_not_politics():
    """A Scryfall miss must not shield — the guard is earned, not
    assumed, or an outage would freeze every cut the advisor can make."""
    assert is_politics_card_name("Nonexistent", lambda n: None) is False


def test_politics_name_lookup_error_is_not_politics():
    """A raising lookup degrades to 'not politics', never propagates:
    the callers are ranking loops."""
    def boom(name):
        raise RuntimeError("scryfall down")
    assert is_politics_card_name("Whatever", boom) is False


# --- per-deck opt-out ------------------------------------------------------

def test_politics_guard_on_by_default():
    """No directive → guard active (decision C2 ships it on)."""
    deck = "[metadata]\nName=Test\nMoxfield=abc\n[Main]\n1 Sol Ring\n"
    assert politics_guard_enabled(deck) is True
    assert politics_guard_enabled("") is True


@pytest.mark.parametrize("value", ["off", "OFF", "false", "no", "0",
                                   "none", "disabled", "  off  "])
def test_politics_guard_opt_out_values(value):
    deck = f"[metadata]\nName=Test\nPoliticsGuard={value}\n[Main]\n"
    assert politics_guard_enabled(deck) is False


@pytest.mark.parametrize("key", ["PoliticsGuard", "politicsguard",
                                 "POLITICSGUARD"])
def test_politics_guard_key_is_case_insensitive(key):
    """Mirrors ``Protect=``'s case-insensitive key."""
    assert politics_guard_enabled(f"[metadata]\n{key}=off\n[Main]\n") is False


def test_politics_guard_explicit_on_is_a_no_op():
    """``PoliticsGuard=on`` is a valid way to state the default."""
    assert politics_guard_enabled("[metadata]\nPoliticsGuard=on\n") is True


def test_politics_guard_unparseable_value_stays_on():
    """Fail SAFE: a typo leaves the shield up rather than silently
    exposing the deck's politics package to margin-driven cuts."""
    assert politics_guard_enabled("[metadata]\nPoliticsGuard=maybe\n") is True
    assert politics_guard_enabled("[metadata]\nPoliticsGuard=\n") is True


def test_politics_guard_ignores_directive_outside_metadata():
    """Only ``[metadata]`` is consulted — same rule as Protect=."""
    deck = "[metadata]\nName=T\n[Main]\nPoliticsGuard=off\n1 Sol Ring\n"
    assert politics_guard_enabled(deck) is True


def test_politics_shield_reason_is_the_project_voice():
    """One sentence, one source of truth: every surface that reports
    the shield quotes this constant verbatim."""
    assert "sim-invisible" in POLITICS_SHIELD_REASON
    assert "A/B margin is not evidence against this card" in (
        POLITICS_SHIELD_REASON)
