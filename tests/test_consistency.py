"""Tests for the opening-hand / mulligan / commander-on-curve module.

Two layers, tested differently:

  * the CLOSED-FORM hypergeometric helpers are checked against
    hand-computed values (P(>=1 of 1 specific card in 7 of 99) = 7/99),
    exact-integer identities (the pmf over all k sums to exactly 1),
    and their documented degenerate cases;
  * the MONTE CARLO is checked for the properties a seeded simulation
    must have -- determinism, agreement with the closed form where the
    two overlap, monotonic response to land count and to commander
    mana value -- plus the fail-quiet outage contract and the
    empty/all-land/no-land edges.

Every card touch is an injected ``lookup`` stub, so the suite is fully
offline. Basic lands deliberately never reach the stub: the module
short-circuits them through ``staples.is_basic_land``.
"""
from __future__ import annotations

import pytest

from commander_builder import consistency
from commander_builder.consistency import (
    KEEPABLE_LAND_MAX,
    KEEPABLE_LAND_MIN,
    format_consistency_report,
    hypergeom_at_least,
    hypergeom_pmf,
    opening_hand_stats,
)


# ---------------------------------------------------------------------------
# Fake card DB -- nothing here reaches Scryfall
# ---------------------------------------------------------------------------

_FAKE_CARDS = {
    # Commanders across the mana-value range, mono and two-color.
    "Red One Drop": {
        "type_line": "Legendary Creature — Goblin",
        "mana_cost": "{R}", "color_identity": ["R"],
    },
    "Red Two Drop": {
        "type_line": "Legendary Creature — Goblin",
        "mana_cost": "{1}{R}", "color_identity": ["R"],
    },
    "Red Seven Drop": {
        "type_line": "Legendary Creature — Dragon",
        "mana_cost": "{5}{R}{R}", "color_identity": ["R"],
    },
    "Azorius Boss": {
        "type_line": "Legendary Creature — Bird",
        "mana_cost": "{W}{U}", "color_identity": ["W", "U"],
    },
    # Spells.
    "Lava Spike": {
        "type_line": "Sorcery", "mana_cost": "{R}", "color_identity": ["R"],
    },
    "Wrath of God": {
        "type_line": "Sorcery", "mana_cost": "{2}{W}{W}",
        "color_identity": ["W"],
    },
    # A cheap WHITE spell -- affordable on turn 3 mana-count alone, so
    # it is the right probe for color screw (a 4-drop would be filtered
    # out by the affordability leg of the definition).
    "Swords to Plowshares": {
        "type_line": "Instant", "mana_cost": "{W}", "color_identity": ["W"],
    },
    "Sol Ring": {
        "type_line": "Artifact", "mana_cost": "{1}", "color_identity": [],
    },
    # A nonbasic dual that taps for both its colors.
    "Test Dual": {
        "type_line": "Land — Plains Island", "mana_cost": "",
        "color_identity": ["W", "U"], "produced_mana": ["W", "U"],
    },
    # A colorless utility land -- a land drop, but no colored source.
    "Test Wasteland": {
        "type_line": "Land", "mana_cost": "", "color_identity": [],
        "produced_mana": ["C"],
    },
    # Spell-front MDFC from deck_health._MDFC_LANDS: front face is a
    # sorcery, back face is a land, so it is land-CAPABLE.
    "Bala Ged Recovery": {
        "type_line": "Sorcery // Land", "mana_cost": "{2}{G}",
        "color_identity": ["G"], "produced_mana": ["G"],
    },
}


def fake_lookup(name):
    """Injected ``lookup``: exact-name map, ``None`` for a miss (the
    same shape ``scryfall_client.lookup_card`` returns on a 404)."""
    return _FAKE_CARDS.get(name)


def _deck(main_lines, commander=None):
    """Build a .dck-format deck text from ``(qty, name)`` pairs."""
    text = "[metadata]\nName=Test Deck\n"
    if commander:
        text += f"[Commander]\n1 {commander}\n"
    text += "[Main]\n"
    for qty, name in main_lines:
        text += f"{qty} {name}\n"
    return text


# Canonical 99: 38 Mountains + 61 red one-drops. Land count sits inside
# deck_health's 33-38 band so it reads as a normal, healthy manabase.
LAND_NORMAL = _deck([(38, "Mountain"), (61, "Lava Spike")], "Red Two Drop")
# Same shell, greedy 20-land manabase -- must score strictly worse.
LAND_LIGHT = _deck([(20, "Mountain"), (79, "Lava Spike")], "Red Two Drop")


# ---------------------------------------------------------------------------
# Layer 1 -- closed-form hypergeometric
# ---------------------------------------------------------------------------

def test_hypergeom_at_least_one_specific_card_in_opening_seven():
    """The canonical Commander sanity check: a singleton deck has ONE
    copy of any given card, so the chance it is among the opening 7 of
    99 is exactly 7/99 -- the draws-over-population ratio."""
    p = hypergeom_at_least(99, 1, 7, 1)
    assert p == pytest.approx(7 / 99, abs=1e-15)


def test_hypergeom_at_least_two_copies_matches_hand_computation():
    """P(>=1 of TWO specific cards in 7 of 99) = 1 - C(97,7)/C(99,7),
    computed independently here from the complement."""
    import math
    expected = 1 - math.comb(97, 7) / math.comb(99, 7)
    assert hypergeom_at_least(99, 2, 7, 1) == pytest.approx(expected, abs=1e-15)


def test_hypergeom_pmf_matches_explicit_binomial_ratio():
    """P(exactly 0 lands in the opening 7 of a 38-land 99) is
    C(61,7)/C(99,7) by definition -- the module must reproduce the
    textbook expression exactly, not approximately."""
    import math
    expected = math.comb(61, 7) / math.comb(99, 7)
    assert hypergeom_pmf(99, 38, 7, 0) == pytest.approx(expected, abs=1e-15)


def test_hypergeom_pmf_sums_to_one_over_all_k():
    """Exact-integer accumulation means the distribution sums to 1 to
    within float epsilon even with the ~1e10 binomials a 99-card deck
    produces. This is the precision guard the docstring promises: a
    float-per-term implementation drifts here."""
    total = sum(hypergeom_pmf(99, 38, 7, k) for k in range(0, 8))
    assert total == pytest.approx(1.0, abs=1e-12)


def test_hypergeom_at_least_degenerate_cases():
    """k=0, no successes, unreachable k, and draws>population all have
    real answers -- none of them is an error."""
    assert hypergeom_at_least(99, 38, 7, 0) == 1.0      # >=0 always true
    assert hypergeom_at_least(99, 0, 7, 1) == 0.0       # nothing to hit
    assert hypergeom_at_least(99, 0, 7, 0) == 1.0
    assert hypergeom_at_least(99, 2, 7, 3) == 0.0       # k > successes
    assert hypergeom_at_least(99, 7, 2, 3) == 0.0       # k > draws
    # draws > population clamps to "draw the whole library", so every
    # success is seen.
    assert hypergeom_at_least(99, 38, 200, 38) == 1.0
    assert hypergeom_pmf(99, 38, 200, 38) == 1.0


def test_hypergeom_incoherent_parameters_return_none_not_zero():
    """Fail-quiet contract at the arithmetic layer: an unanswerable
    question returns None. 0.0 is a valid probability and would be
    silently believed by a caller."""
    assert hypergeom_at_least(-1, 1, 7, 1) is None
    assert hypergeom_at_least(99, -1, 7, 1) is None
    assert hypergeom_at_least(99, 1, -7, 1) is None
    assert hypergeom_at_least(99, 200, 7, 1) is None   # successes > population
    assert hypergeom_pmf(99, 200, 7, 1) is None


def test_hypergeom_at_least_is_monotone_decreasing_in_k():
    """P(>=k) can only fall as k rises -- a cheap structural invariant
    that catches a mis-summed tail."""
    ps = [hypergeom_at_least(99, 38, 7, k) for k in range(0, 8)]
    assert all(ps[i] >= ps[i + 1] for i in range(len(ps) - 1))
    assert all(0.0 <= p <= 1.0 for p in ps)


# ---------------------------------------------------------------------------
# Layer 2 -- determinism (hard requirement: feeds the FP-002 dataset)
# ---------------------------------------------------------------------------

def test_same_seed_gives_identical_dict():
    """Same seed + same deck => byte-identical result. Without this the
    regression dataset cannot tell a deck change from RNG drift."""
    a = opening_hand_stats(LAND_NORMAL, trials=300, seed=7, lookup=fake_lookup)
    b = opening_hand_stats(LAND_NORMAL, trials=300, seed=7, lookup=fake_lookup)
    assert a == b


def test_different_seeds_agree_within_monte_carlo_noise():
    """Different seeds must move the numbers (it is a simulation) but
    only inside sampling noise -- a large gap would mean the seed is
    doing more than seeding."""
    a = opening_hand_stats(LAND_NORMAL, trials=800, seed=1, lookup=fake_lookup)
    b = opening_hand_stats(LAND_NORMAL, trials=800, seed=2, lookup=fake_lookup)
    assert a["p_keepable_7"] == pytest.approx(b["p_keepable_7"], abs=0.08)


def test_result_does_not_depend_on_global_random_state():
    """The module must use ``random.Random(seed)``, never the global
    ``random``. Perturbing the global RNG between runs must not move a
    single digit."""
    import random as _random
    _random.seed(1)
    a = opening_hand_stats(LAND_NORMAL, trials=200, seed=3, lookup=fake_lookup)
    _random.seed(999)
    [_random.random() for _ in range(50)]
    b = opening_hand_stats(LAND_NORMAL, trials=200, seed=3, lookup=fake_lookup)
    assert a == b


# ---------------------------------------------------------------------------
# Layer 2 -- agreement with the closed form
# ---------------------------------------------------------------------------

def test_avg_lands_in_7_matches_hypergeometric_expectation():
    """E[lands in 7] = 7 * L/N exactly. The simulation is only a valid
    instrument if it reproduces the one moment we can derive."""
    stats = opening_hand_stats(
        LAND_NORMAL, trials=4000, seed=11, lookup=fake_lookup,
    )
    assert stats["avg_lands_in_7"] == pytest.approx(7 * 38 / 99, abs=0.06)


def test_p_keepable_7_matches_closed_form_band():
    """p_keepable_7 is P(2 <= lands <= 5), which the closed-form layer
    can compute directly as at_least(2) - at_least(6). Cross-checking
    the two layers against each other pins the keep RULE, not just the
    sampler."""
    closed = (
        hypergeom_at_least(99, 38, 7, KEEPABLE_LAND_MIN)
        - hypergeom_at_least(99, 38, 7, KEEPABLE_LAND_MAX + 1)
    )
    stats = opening_hand_stats(
        LAND_NORMAL, trials=4000, seed=5, lookup=fake_lookup,
    )
    assert stats["p_keepable_7"] == pytest.approx(closed, abs=0.03)
    assert stats["mulligan_rate"] == pytest.approx(
        1 - stats["p_keepable_7"], abs=1e-12,
    )


# ---------------------------------------------------------------------------
# Layer 2 -- land count drives consistency
# ---------------------------------------------------------------------------

def test_land_light_deck_scores_worse_than_land_normal():
    """A 20-land 99 must be strictly worse than a 38-land 99 on every
    consistency axis. This is the headline claim of the module."""
    light = opening_hand_stats(
        LAND_LIGHT, trials=1500, seed=4, lookup=fake_lookup,
    )
    normal = opening_hand_stats(
        LAND_NORMAL, trials=1500, seed=4, lookup=fake_lookup,
    )
    assert light["avg_lands_in_7"] < normal["avg_lands_in_7"]
    assert light["p_keepable_7"] < normal["p_keepable_7"]
    assert light["mulligan_rate"] > normal["mulligan_rate"]
    assert light["p_3_lands_by_t3"] < normal["p_3_lands_by_t3"]
    assert light["p_5_lands_by_t5"] < normal["p_5_lands_by_t5"]
    assert light["p_commander_on_curve"] < normal["p_commander_on_curve"]


def test_on_the_draw_beats_on_the_play():
    """The extra card is worth real percentage points, and both
    conventions are always returned. Same shuffles back both, so this
    comparison is paired, not two independent samples."""
    stats = opening_hand_stats(
        LAND_NORMAL, trials=1500, seed=6, lookup=fake_lookup,
    )
    play, draw = stats["on_play"], stats["on_draw"]
    assert draw["p_3_lands_by_t3"] > play["p_3_lands_by_t3"]
    assert draw["p_5_lands_by_t5"] > play["p_5_lands_by_t5"]
    # Top-level aliases quote the documented default convention.
    assert stats["convention"] == "on_play"
    assert stats["p_3_lands_by_t3"] == stats["on_play"]["p_3_lands_by_t3"]
    assert stats["p_5_lands_by_t5"] == stats["on_play"]["p_5_lands_by_t5"]


def test_land_checkpoints_are_nested():
    """5 lands by turn 5 implies 3 by turn 3 far less often than the
    reverse, so p_3_by_t3 must dominate p_5_by_t5 on the same deck."""
    stats = opening_hand_stats(
        LAND_NORMAL, trials=1500, seed=8, lookup=fake_lookup,
    )
    assert stats["p_3_lands_by_t3"] > stats["p_5_lands_by_t5"]


# ---------------------------------------------------------------------------
# Layer 2 -- commander on curve (a MANA question, not a DRAW question)
# ---------------------------------------------------------------------------

def test_commander_on_curve_falls_as_mana_value_rises():
    """The metric must respond to the commander's mana value: a 1-drop
    is nearly free, a 2-drop is easy, a 7-drop is a project."""
    one = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Red One Drop"),
        trials=1200, seed=2, lookup=fake_lookup,
    )
    two = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Red Two Drop"),
        trials=1200, seed=2, lookup=fake_lookup,
    )
    seven = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Red Seven Drop"),
        trials=1200, seed=2, lookup=fake_lookup,
    )
    assert one["commander_mana_value"] == 1
    assert seven["commander_mana_value"] == 7
    assert one["p_commander_on_curve"] > two["p_commander_on_curve"]
    assert two["p_commander_on_curve"] > seven["p_commander_on_curve"]


def test_commander_on_curve_ignores_whether_the_commander_was_drawn():
    """THE distinction the metric exists to get right: the commander is
    always available from the command zone, so a 1-mana commander that
    appears NOWHERE in the 99 is still castable on turn 1 whenever a
    red source is in play. A draw-based implementation would score this
    near 1/99 instead of near-certain."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Red One Drop"),
        trials=1200, seed=3, lookup=fake_lookup,
    )
    assert "Red One Drop" not in stats  # not in the library, by construction
    assert stats["p_commander_on_curve"] > 0.9


def test_commander_on_curve_requires_the_right_colors():
    """An all-Mountain manabase can never cast a {W}{U} commander, no
    matter how many lands it hits. Zero, not "roughly on curve"."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Azorius Boss"),
        trials=500, seed=9, lookup=fake_lookup,
    )
    assert stats["p_commander_on_curve"] == 0.0


def test_commander_on_curve_none_when_commander_unresolvable():
    """Outage contract at the metric level: no resolvable commander
    means no mana value, and an invented on-curve turn would be the
    fabricated number the module forbids. None, never 0.0."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Nonexistent General"),
        trials=200, seed=1, lookup=fake_lookup,
    )
    assert stats is not None
    assert stats["p_commander_on_curve"] is None
    assert stats["commander_mana_value"] is None
    # The rest of the profile still computes -- one unavailable metric
    # must not take the panel down with it.
    assert stats["p_keepable_7"] > 0.0


def test_commander_on_curve_none_when_no_commander_section():
    """Same contract when the deck simply has no [Commander] line."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")]),
        trials=200, seed=1, lookup=fake_lookup,
    )
    assert stats["commander"] is None
    assert stats["p_commander_on_curve"] is None


def test_dual_land_pays_only_one_pip():
    """A dual taps ONCE: {W}{U} off a single dual is not castable, off
    two duals it is. This is why pip feasibility is a bipartite
    matching and not a per-color source count."""
    wu = (("U", 1), ("W", 1))
    one_dual = [frozenset({"W", "U"})]
    two_duals = [frozenset({"W", "U"}), frozenset({"W", "U"})]
    assert consistency._pips_payable(wu, one_dual) is False
    assert consistency._pips_payable(wu, two_duals) is True
    # A matching that needs the augmenting path: the only W source also
    # happens to be the only U source's partner.
    assert consistency._pips_payable(
        wu, [frozenset({"W"}), frozenset({"W", "U"})],
    ) is True
    assert consistency._pips_payable(
        wu, [frozenset({"W"}), frozenset({"W"})],
    ) is False
    # No colored pips -> always payable (generic is the caller's
    # lands >= mana_value check).
    assert consistency._pips_payable((), []) is True


def test_colorless_lands_hit_land_drops_but_not_colors():
    """A Wastes-style land is a real land drop and a real 0 colored
    sources -- the deck hits 3-by-3 yet can never cast its commander."""
    stats = opening_hand_stats(
        _deck([(38, "Test Wasteland"), (61, "Sol Ring")], "Red Two Drop"),
        trials=600, seed=12, lookup=fake_lookup,
    )
    assert stats["land_count"] == 38
    assert stats["p_3_lands_by_t3"] > 0.3
    assert stats["p_commander_on_curve"] == 0.0


# ---------------------------------------------------------------------------
# Layer 2 -- color screw
# ---------------------------------------------------------------------------

def test_color_screw_flagged_when_lands_are_the_wrong_color():
    """Mountains in play, white one-drops in hand: the mana is there
    and nothing is castable. That is color screw, distinct from mana
    screw."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Swords to Plowshares")],
              "Red Two Drop"),
        trials=800, seed=13, lookup=fake_lookup,
    )
    assert stats["p_color_screw"] > 0.3


def test_color_screw_ignores_spells_you_could_not_cast_anyway():
    """The affordability leg matters: a hand full of 4-drops on turn 3
    is not color screw, it is just turn 3. Only spells whose MANA COUNT
    is already met can be blamed on colors."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Wrath of God")], "Red Two Drop"),
        trials=500, seed=13, lookup=fake_lookup,
    )
    assert stats["p_color_screw"] == 0.0


def test_color_screw_absent_when_colors_line_up():
    """Same shell with on-color spells: never screwed, by construction."""
    stats = opening_hand_stats(
        LAND_NORMAL, trials=800, seed=13, lookup=fake_lookup,
    )
    assert stats["p_color_screw"] == 0.0


def test_color_screw_not_triggered_by_missing_lands():
    """A deck that never makes its turn-3 drop is mana screwed, not
    color screwed -- the definition requires the lands to be THERE, so
    the two failure modes stay separable."""
    stats = opening_hand_stats(
        _deck([(99, "Wrath of God")], "Red Two Drop"),
        trials=300, seed=14, lookup=fake_lookup,
    )
    assert stats["p_color_screw"] == 0.0


# ---------------------------------------------------------------------------
# Fail-quiet outage contract (mirrors deck_health)
# ---------------------------------------------------------------------------

def test_returns_none_when_majority_of_lookups_fail():
    """``deck_health``'s exact majority-failure guard: more than half
    the card lines unresolved means "cannot measure this deck", which
    must not render as a deck with zero lands."""
    deck = _deck([(30, "Mystery A"), (30, "Mystery B"), (39, "Mystery C")])
    assert opening_hand_stats(
        deck, trials=100, seed=0, lookup=lambda name: None,
    ) is None


def test_returns_none_when_lookup_raises():
    """A hard exception from the lookup layer degrades to the same
    unavailable answer -- never a traceback out of a report path."""
    def _boom(name):
        raise RuntimeError("scryfall down")

    deck = _deck([(99, "Mystery A")])
    assert opening_hand_stats(deck, trials=100, seed=0, lookup=_boom) is None


def test_minority_lookup_failures_still_compute_and_are_surfaced():
    """At or below the threshold the profile still computes; the miss
    count rides along as ``lookup_failures`` so a caller can annotate,
    and the unresolved card stays in the library (deck size is a
    structural fact) as a non-land, which biases the numbers low
    rather than inventing a success."""
    deck = _deck(
        [(38, "Mountain"), (1, "Mystery Card"), (60, "Lava Spike")],
        "Red Two Drop",
    )
    stats = opening_hand_stats(deck, trials=300, seed=0, lookup=fake_lookup)
    assert stats is not None
    assert stats["lookup_failures"] == 1
    assert stats["deck_size"] == 99      # printed size preserved
    assert stats["land_count"] == 38     # the miss did not become a land


def test_empty_deck_returns_none():
    """Nothing to measure -- same shape as ``_mana_health_signal``'s
    empty-deck guard."""
    assert opening_hand_stats("", trials=100, seed=0, lookup=fake_lookup) is None
    assert opening_hand_stats(
        "[metadata]\nName=X\n[Commander]\n1 Red Two Drop\n",
        trials=100, seed=0, lookup=fake_lookup,
    ) is None


def test_zero_trials_returns_none():
    """Zero trials cannot produce an estimate; averaging over an empty
    sample would be a fabricated 0.0."""
    assert opening_hand_stats(
        LAND_NORMAL, trials=0, seed=0, lookup=fake_lookup,
    ) is None


# ---------------------------------------------------------------------------
# Edge cases -- all lands / no lands / MDFC
# ---------------------------------------------------------------------------

def test_all_lands_deck():
    """99 Mountains: every 7 holds 7 lands, which is outside the 2-5
    keep band, so the deck mulligans every game and (under London
    bottoming) keeps 5 at the floor. Land drops are then automatic."""
    stats = opening_hand_stats(
        _deck([(99, "Mountain")], "Red Two Drop"),
        trials=200, seed=0, lookup=fake_lookup,
    )
    assert stats["land_count"] == 99
    assert stats["avg_lands_in_7"] == 7.0
    assert stats["p_keepable_7"] == 0.0
    assert stats["mulligan_rate"] == 1.0
    assert stats["avg_opening_hand_size"] == 7.0 - consistency.MAX_MULLIGANS
    assert stats["p_3_lands_by_t3"] == 1.0
    assert stats["p_5_lands_by_t5"] == 1.0
    assert stats["p_commander_on_curve"] == 1.0


def test_no_lands_deck():
    """0 lands: no keepable hand exists, no land ever hits play, and
    the commander is never castable. All the failures, none of them
    crashes."""
    stats = opening_hand_stats(
        _deck([(99, "Lava Spike")], "Red Two Drop"),
        trials=200, seed=0, lookup=fake_lookup,
    )
    assert stats["land_count"] == 0
    assert stats["avg_lands_in_7"] == 0.0
    assert stats["p_keepable_7"] == 0.0
    assert stats["mulligan_rate"] == 1.0
    assert stats["p_3_lands_by_t3"] == 0.0
    assert stats["p_5_lands_by_t5"] == 0.0
    assert stats["p_commander_on_curve"] == 0.0
    assert stats["p_color_screw"] == 0.0


def test_mdfc_spell_front_counts_as_a_land_drop_but_not_a_printed_land():
    """The one deliberate divergence from ``_mana_health_signal``: a
    spell-front MDFC is a FULL land for "can I make this drop" (the
    simulation), while the reported ``effective_land_count`` keeps that
    module's 0.5 deck-construction weighting so the two reconcile."""
    stats = opening_hand_stats(
        _deck([(30, "Mountain"), (8, "Bala Ged Recovery"),
               (61, "Lava Spike")], "Red Two Drop"),
        trials=800, seed=0, lookup=fake_lookup,
    )
    assert stats["land_count"] == 30            # strict front-face lands
    assert stats["mdfc_land_count"] == 8
    assert stats["effective_land_count"] == 34.0  # 30 + 0.5*8
    # 38 land-capable cards => same opening-hand land density as the
    # canonical 38-land deck.
    assert stats["avg_lands_in_7"] == pytest.approx(7 * 38 / 99, abs=0.12)


def test_tiny_deck_does_not_crash():
    """Fewer cards than an opening hand is degenerate but must degrade,
    not raise -- report paths run on partially-built decks."""
    stats = opening_hand_stats(
        _deck([(3, "Mountain")], "Red Two Drop"),
        trials=50, seed=0, lookup=fake_lookup,
    )
    assert stats is not None
    assert stats["deck_size"] == 3
    assert 0.0 <= stats["p_keepable_7"] <= 1.0


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def test_format_consistency_report_renders_the_headline_numbers():
    stats = opening_hand_stats(
        LAND_NORMAL, trials=300, seed=0, lookup=fake_lookup,
    )
    text = format_consistency_report(stats)
    assert "Consistency check" in text
    assert "Red Two Drop" in text
    assert "Keepable 7" in text
    assert "Mulligan rate" in text
    assert "Commander on curve" in text
    assert "on the play" in text and "on the draw" in text
    assert "%" in text


def test_format_consistency_report_handles_unavailable():
    """The None contract has to survive rendering: an outage prints one
    honest line, not a table of confident zeroes."""
    text = format_consistency_report(None)
    assert "unavailable" in text
    assert "0.0%" not in text


def test_format_consistency_report_marks_unavailable_metrics():
    """A resolvable deck with an unresolvable commander renders the
    commander row as 'unavailable' while the rest of the table prints
    normally."""
    stats = opening_hand_stats(
        _deck([(38, "Mountain"), (61, "Lava Spike")], "Nonexistent General"),
        trials=200, seed=0, lookup=fake_lookup,
    )
    text = format_consistency_report(stats)
    assert "unavailable" in text
    assert "3 lands by turn 3" in text


def test_format_consistency_report_notes_lookup_failures():
    deck = _deck(
        [(38, "Mountain"), (1, "Mystery Card"), (60, "Lava Spike")],
        "Red Two Drop",
    )
    stats = opening_hand_stats(deck, trials=200, seed=0, lookup=fake_lookup)
    text = format_consistency_report(stats)
    assert "unresolved" in text
