"""Tests for deck-health role-target ratios (F2)."""
from __future__ import annotations

import pytest

from commander_builder import staples, deck_health
from commander_builder.staples import ROLE_TARGETS, role_target_report


def _report_from_counts(counts, commander_names=None):
    """role_target_report over a stubbed count_deck_roles.

    ``role_target_report`` resolves ``count_deck_roles`` as a module
    global at call time, so swapping the attribute (and restoring it in
    a ``finally``) is enough to make the report deterministic without
    touching Scryfall.
    """
    original = staples.count_deck_roles
    staples.count_deck_roles = lambda names: dict(counts)
    try:
        return role_target_report(["x"], commander_names)
    finally:
        staples.count_deck_roles = original


def test_role_target_report_flags_deficits(monkeypatch):
    # ramp 5/10, draw 12/10, removal 8/8, wipe 0/3, protection 0/4,
    # finisher 0/3 (the deck can't win — see the finisher tests below)
    monkeypatch.setattr(staples, "count_deck_roles",
                        lambda names: {"ramp": 5, "draw": 12, "removal": 8, "wipe": 0})
    r = role_target_report(["x"])
    assert r["roles"]["ramp"]["deficit"] == 5
    assert r["roles"]["draw"]["deficit"] == 0      # over target → no deficit
    assert r["roles"]["removal"]["deficit"] == 0   # exactly at target
    assert r["roles"]["wipe"]["deficit"] == 3
    assert r["roles"]["protection"]["deficit"] == 4
    assert r["roles"]["finisher"]["deficit"] == 3
    # under_built sorted worst-deficit first, ties in ROLE_TARGETS order:
    # ramp(5), protection(4), wipe(3), finisher(3).
    assert r["under_built"] == ["ramp", "protection", "wipe", "finisher"]


def test_role_target_report_well_built(monkeypatch):
    # NOTE: 'well built' now REQUIRES win conditions. Before the finisher
    # target existed this same fixture (ramp/draw/removal/wipe/protection
    # only) passed, which is exactly the hole being closed — a deck with
    # no way to win was indistinguishable from a complete one.
    monkeypatch.setattr(staples, "count_deck_roles",
                        lambda names: {"ramp": 11, "draw": 11, "removal": 9,
                                       "wipe": 4, "protection": 5,
                                       "finisher": 3})
    r = role_target_report(["x"])
    assert r["under_built"] == []
    assert all(v["deficit"] == 0 for v in r["roles"].values())


def test_role_targets_cover_expected_roles():
    assert set(ROLE_TARGETS) == {
        "ramp", "draw", "removal", "wipe", "protection", "finisher",
    }


def test_ramp_and_draw_only_deck_is_under_built():
    """The headline hole: a deck of nothing but ramp and card draw used
    to clear EVERY role target. With the finisher target it can't —
    removal, wipes, protection AND the ability to win are all missing."""
    counts = {"ramp": 40, "draw": 59}
    r = _report_from_counts(counts)
    assert r["roles"]["finisher"]["deficit"] == 3
    assert set(r["under_built"]) == {"removal", "protection", "wipe", "finisher"}


def test_finisher_target_counts_win_condition_bucket():
    """``classify_role`` says 'finisher', ``classify_role_extended`` says
    'win_condition', and both mean 'this card wins the game' — so both
    count toward the single finisher target. Craterhoof (win_condition)
    plus two Exsanguinate-alikes (finisher) satisfies it."""
    r = _report_from_counts({"finisher": 2, "win_condition": 1})
    assert r["roles"]["finisher"]["count"] == 3
    assert r["roles"]["finisher"]["deficit"] == 0
    assert "finisher" not in r["under_built"]


# ---------------------------------------------------------------------------
# Commander role credit — the commander is not just another card
# ---------------------------------------------------------------------------

def _commander_lookup(monkeypatch, oracle_text, type_line="Legendary Creature"):
    """Stub staples.lookup_card so the commander classifies offline."""
    monkeypatch.setattr(
        staples, "lookup_card",
        lambda name: {"oracle_text": oracle_text, "type_line": type_line},
    )


def test_commander_that_draws_cards_reduces_the_draw_target(monkeypatch):
    """An Edric-class commander IS the draw engine. Before the credit,
    such a deck was told to add 10 card-draw spells on top of a
    commander that draws every combat."""
    _commander_lookup(
        monkeypatch,
        "Whenever a creature deals combat damage to one of your "
        "opponents, that creature's controller may draw a card.",
    )
    r = _report_from_counts({"draw": 6}, commander_names=["Edric"])
    assert r["roles"]["draw"]["base_target"] == 10
    assert r["roles"]["draw"]["commander_credit"] == staples.COMMANDER_ROLE_CREDIT
    assert r["roles"]["draw"]["target"] == 8
    assert r["roles"]["draw"]["deficit"] == 2      # was 4 without the credit
    # Roles the commander doesn't fill keep their full target.
    assert r["roles"]["ramp"]["target"] == 10


def test_voltron_commander_credits_the_finisher_target(monkeypatch):
    """A commander that IS the win condition classifies as
    ``win_condition`` and folds into the finisher target — the deck no
    longer needs three separate haymakers to clear it."""
    _commander_lookup(monkeypatch, "You win the game.")
    r = _report_from_counts({"finisher": 1}, commander_names=["Test Voltron"])
    assert r["roles"]["finisher"]["commander_credit"] == 2
    assert r["roles"]["finisher"]["target"] == 1
    assert r["roles"]["finisher"]["deficit"] == 0


def test_commander_credit_absent_without_a_command_zone(monkeypatch):
    """No commander names → the exact pre-2026-07 shape: full targets,
    zero credit. Every caller that only has a card-name list is here."""
    r = _report_from_counts({"draw": 6})
    assert r["roles"]["draw"]["target"] == 10
    assert r["roles"]["draw"]["commander_credit"] == 0
    assert r["roles"]["draw"]["deficit"] == 4


def test_commander_credit_fails_quiet_on_lookup_error(monkeypatch):
    """A Scryfall blip on the commander degrades to 'no credit' (today's
    behavior), never to a crash or a fabricated credit."""
    def boom(name):
        raise RuntimeError("scryfall down")
    monkeypatch.setattr(staples, "lookup_card", boom)
    r = _report_from_counts({"draw": 6}, commander_names=["Edric"])
    assert r["roles"]["draw"]["target"] == 10
    assert r["roles"]["draw"]["commander_credit"] == 0


def test_deck_health_signal_wires_in(monkeypatch):
    monkeypatch.setattr("commander_builder.staples.count_deck_roles",
                        lambda names: {"removal": 3})
    sig = deck_health._role_targets_signal(
        "[Main]\n1 Swords to Plowshares\n1 Path to Exile\n")
    assert sig["roles"]["removal"]["deficit"] == 5  # 3 vs target 8
    assert "removal" in sig["under_built"]


def test_deck_health_signal_threads_the_commander(monkeypatch):
    """``_role_targets_signal`` must pass the [Commander] section through
    — the whole point of the credit is that the audit panel stops asking
    an Edric deck for 10 draw spells."""
    monkeypatch.setattr("commander_builder.staples.count_deck_roles",
                        lambda names: {"draw": 6})
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card",
        lambda name: {"oracle_text": "Draw a card.",
                      "type_line": "Legendary Creature"},
    )
    sig = deck_health._role_targets_signal(
        "[Commander]\n1 Edric, Spymaster of Trest\n"
        "[Main]\n1 Rhystic Study\n")
    assert sig["roles"]["draw"]["commander_credit"] == 2
    assert sig["roles"]["draw"]["target"] == 8
    assert sig["roles"]["draw"]["deficit"] == 2


def test_deck_health_signal_degrades_on_error(monkeypatch):
    def boom(names):
        raise RuntimeError("scryfall down")
    monkeypatch.setattr("commander_builder.staples.count_deck_roles", boom)
    sig = deck_health._role_targets_signal("[Main]\n1 Sol Ring\n")
    assert sig == {"roles": {}, "under_built": []}


# ---------------------------------------------------------------------------
# FP-019.3 -- context-sensitive quotas (primer heuristics §3/§4)
# ---------------------------------------------------------------------------

from commander_builder.staples import (  # noqa: E402
    contextual_role_targets,
    infer_commander_role,
)


def test_contextual_targets_default_equals_flat_table():
    assert contextual_role_targets() == ROLE_TARGETS


def test_contextual_targets_aggro_reshapes_edgar_style():
    # Edgar spec (§4): 15 CA + 12 interaction, rocks out of the deck.
    t = contextual_role_targets(archetype="aggro")
    assert t["draw"] > ROLE_TARGETS["draw"]
    assert t["removal"] > ROLE_TARGETS["removal"]
    assert t["ramp"] < ROLE_TARGETS["ramp"]


def test_contextual_targets_bracket_raises_interaction_floor():
    b3 = contextual_role_targets(bracket=3)
    b5 = contextual_role_targets(bracket=5)
    assert b3 == ROLE_TARGETS  # mid bracket: no delta
    assert b5["removal"] > ROLE_TARGETS["removal"]
    assert b5["protection"] > ROLE_TARGETS["protection"]


def test_contextual_targets_avg_mv_moves_ramp_both_ways():
    low = contextual_role_targets(avg_mv=2.4)
    high = contextual_role_targets(avg_mv=4.6)
    assert low["ramp"] < ROLE_TARGETS["ramp"]    # "lands don't cost 2 mana"
    assert high["ramp"] > ROLE_TARGETS["ramp"]   # Ur-Dragon oversize ramp


def test_contextual_targets_commander_role_engine():
    t = contextual_role_targets(commander_role="resolve_engine")
    assert t["ramp"] > ROLE_TARGETS["ramp"]
    assert t["protection"] > ROLE_TARGETS["protection"]


def test_contextual_targets_unknown_context_is_noop_and_clamped():
    assert contextual_role_targets(archetype="jazz-fusion") == ROLE_TARGETS
    assert contextual_role_targets(commander_role="dj") == ROLE_TARGETS
    stacked = contextual_role_targets(
        archetype="aggro", commander_role="trigger_multiplier", avg_mv=2.0)
    assert all(v >= 0 for v in stacked.values())


def test_role_target_report_accepts_context(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.staples.count_deck_roles", lambda names: {})
    flat = role_target_report(["Sol Ring"])
    ctx = role_target_report(["Sol Ring"], context={"archetype": "aggro"})
    assert flat["roles"]["draw"]["target"] == ROLE_TARGETS["draw"]
    assert ctx["roles"]["draw"]["base_target"] > ROLE_TARGETS["draw"]
    assert ctx["roles"]["draw"]["context_delta"] > 0
    # shape stays a superset of the flat report's keys
    assert set(flat["roles"]["draw"]) <= set(ctx["roles"]["draw"])


def test_role_target_report_context_none_is_byte_identical(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.staples.count_deck_roles", lambda names: {"ramp": 4})
    assert role_target_report(["x"]) == role_target_report(["x"], context=None)


def test_infer_commander_role_cost_cheater():
    krrik = ("Once during each of your turns, you may pay 2 life rather "
             "than pay the mana cost for a black spell you cast.")
    assert infer_commander_role(krrik, "Legendary Creature", 3.0) \
        == "cost_cheater"


def test_infer_commander_role_trigger_multiplier():
    winota = ("Whenever a non-Human creature you control attacks, look at "
              "the top six cards of your library.")
    assert infer_commander_role(winota, "Legendary Creature", 4.0) \
        == "trigger_multiplier"


def test_infer_commander_role_resolve_engine_by_weight():
    ghalta = "Trample"
    assert infer_commander_role(ghalta, "Legendary Creature", 12.0) \
        == "resolve_engine"


def test_infer_commander_role_none_when_unsure():
    assert infer_commander_role("Vigilance", "Legendary Creature", 3.0) is None
    assert infer_commander_role("", "", None) is None
