"""consistency_targets unit tests — FP-019.2 named primer-derived floors.

All card data is injected via a stub lookup; nothing touches the network.
"""
import pytest

from commander_builder.consistency import hypergeom_at_least
from commander_builder.consistency_targets import (
    CARDS_SEEN_BY_T5_ON_PLAY,
    CONSISTENCY_TARGETS,
    evaluate_consistency_targets,
)


# --- fixtures ----------------------------------------------------------------

def _card(type_line="Instant", oracle="", cmc=2.0):
    return {"type_line": type_line, "oracle_text": oracle, "cmc": cmc}


STUB = {
    "Forest": _card("Basic Land — Forest", "({T}: Add {G}.)", 0.0),
    "Divination": _card("Sorcery", "Draw two cards.", 3.0),
    "Demonic Tutor": _card(
        "Sorcery", "Search your library for a card, put that card into "
        "your hand, then shuffle.", 2.0),
    "Llanowar Elves": _card("Creature — Elf Druid", "{T}: Add {G}.", 1.0),
    "Dark Ritual": _card("Instant", "Add {B}{B}{B}.", 1.0),
    "Canopy Vista": _card(
        "Land — Forest Plains", "Canopy Vista enters the battlefield "
        "tapped.", 0.0),
    "Temple Garden": _card(
        "Land — Forest Plains", "As Temple Garden enters the battlefield, "
        "you may pay 2 life. If you don't, it enters the battlefield "
        "tapped.", 0.0),
    "Grizzly Bears": _card("Creature — Bear", "", 2.0),
}


def _lookup(name):
    return STUB.get(name)


def _deck(lines):
    return "[Commander]\n1 Grizzly Bears\n[Main]\n" + "\n".join(lines) + "\n"


def _fill(lines, target=99, filler="Grizzly Bears"):
    """Pad a line list with filler up to ``target`` total cards."""
    count = sum(int(l.split(" ", 1)[0]) for l in lines)
    return lines + [f"{target - count} {filler}"]


CONSISTENCY_OK = {"p_3_lands_by_t3": 0.90, "p_commander_on_curve": 0.88}


# --- outage contract ---------------------------------------------------------

def test_empty_deck_returns_none():
    assert evaluate_consistency_targets("", lookup=_lookup) is None


def test_majority_lookup_failure_returns_none():
    deck = _deck(["50 Unknown Card A", "49 Forest"])
    # one of two unique names fails -> not a majority; three of four is.
    deck = _deck(["30 Unknown A", "30 Unknown B", "30 Unknown C", "9 Forest"])
    assert evaluate_consistency_targets(deck, lookup=_lookup) is None


# --- unconditional checks ----------------------------------------------------

def test_third_land_drop_met_and_missed():
    deck = _deck(_fill(["30 Forest"]))
    good = evaluate_consistency_targets(
        deck, consistency={"p_3_lands_by_t3": 0.90}, lookup=_lookup)
    bad = evaluate_consistency_targets(
        deck, consistency={"p_3_lands_by_t3": 0.70}, lookup=_lookup)
    assert good["checks"]["third_land_drop"]["met"] is True
    assert bad["checks"]["third_land_drop"]["met"] is False
    assert bad["checks"]["third_land_drop"]["target"] == 0.85


def test_missing_consistency_projection_leaves_met_none():
    deck = _deck(_fill(["30 Forest"]))
    out = evaluate_consistency_targets(deck, consistency=None, lookup=_lookup)
    assert out["checks"]["third_land_drop"]["met"] is None
    assert out["checks"]["third_land_drop"]["value"] is None


def test_commander_on_curve_none_stays_none():
    deck = _deck(_fill(["30 Forest"]))
    out = evaluate_consistency_targets(
        deck, consistency={"p_commander_on_curve": None}, lookup=_lookup)
    assert out["checks"]["commander_on_curve"]["met"] is None


def test_card_advantage_uses_hypergeometric_over_t5_window():
    lines = _fill(["14 Divination", "4 Demonic Tutor", "30 Forest"])
    deck = _deck(lines)
    out = evaluate_consistency_targets(deck, lookup=_lookup)
    check = out["checks"]["card_advantage_by_t5"]
    expected = hypergeom_at_least(99, 18, CARDS_SEEN_BY_T5_ON_PLAY, 1)
    assert check["value"] == pytest.approx(expected)
    assert check["met"] is (expected >= 0.90)


def test_card_advantage_thin_draw_fails():
    deck = _deck(_fill(["2 Divination", "40 Forest"]))
    out = evaluate_consistency_targets(deck, lookup=_lookup)
    assert out["checks"]["card_advantage_by_t5"]["met"] is False


# --- conditional checks ------------------------------------------------------

def test_t1_enablers_reports_but_does_not_judge_without_plan():
    deck = _deck(_fill(["8 Llanowar Elves", "5 Dark Ritual", "30 Forest"]))
    out = evaluate_consistency_targets(deck, lookup=_lookup)
    check = out["checks"]["t1_enablers"]
    assert check["value"] == 13
    assert check["applies"] is False
    assert check["met"] is None
    assert 0.0 < check["p_with_free_mulligan"] < 1.0


def test_t1_enablers_judged_when_plan_declared():
    lines = ["8 Llanowar Elves", "5 Dark Ritual", "30 Forest"]
    ok = evaluate_consistency_targets(
        _deck(_fill(lines)), lookup=_lookup, plans={"t1_enabler_plan"})
    assert ok["checks"]["t1_enablers"]["applies"] is True
    assert ok["checks"]["t1_enablers"]["met"] is True
    thin = evaluate_consistency_targets(
        _deck(_fill(["8 Llanowar Elves", "30 Forest"])),
        lookup=_lookup, plans={"t1_enabler_plan"})
    assert thin["checks"]["t1_enablers"]["met"] is False


def test_free_mulligan_math_matches_primer_benchmark():
    # §1: 13 enablers = 63.9% in the opener, 87% counting the free mull.
    deck = _deck(_fill(["8 Llanowar Elves", "5 Dark Ritual", "30 Forest"]))
    out = evaluate_consistency_targets(deck, lookup=_lookup)
    check = out["checks"]["t1_enablers"]
    p1 = hypergeom_at_least(99, 13, 7, 1)
    assert check["p_with_free_mulligan"] == pytest.approx(1 - (1 - p1) ** 2)


def test_tapped_fetchables_counts_unconditional_only():
    lines = _fill(["3 Canopy Vista", "2 Temple Garden", "30 Forest"])
    out = evaluate_consistency_targets(
        _deck(lines), lookup=_lookup, plans={"proactive_t2_plan"})
    check = out["checks"]["tapped_fetchables"]
    assert check["value"] == 3  # Temple Garden's tapped is conditional
    assert check["applies"] is True
    assert check["met"] is False  # 3 > the ≤2 target
    two = evaluate_consistency_targets(
        _deck(_fill(["2 Canopy Vista", "30 Forest"])),
        lookup=_lookup, plans={"proactive_t2_plan"})
    assert two["checks"]["tapped_fetchables"]["met"] is True


def test_tapped_fetchables_informational_without_plan():
    out = evaluate_consistency_targets(
        _deck(_fill(["3 Canopy Vista", "30 Forest"])), lookup=_lookup)
    check = out["checks"]["tapped_fetchables"]
    assert check["value"] == 3 and check["met"] is None


# --- report shape ------------------------------------------------------------

def test_summary_counts_and_targets_table_agree():
    deck = _deck(_fill(["14 Divination", "4 Demonic Tutor", "30 Forest"]))
    out = evaluate_consistency_targets(
        deck, consistency=CONSISTENCY_OK, lookup=_lookup)
    assert set(out["checks"]) == set(CONSISTENCY_TARGETS)
    applicable = [c for c in out["checks"].values() if c["met"] is not None]
    assert out["evaluated"] == len(applicable)
    assert out["met"] == sum(1 for c in applicable if c["met"])
    assert out["lookup_failures"] == 0


# --- deck_health wiring ------------------------------------------------------

def _stub_scryfall(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: {"name": name, "type_line": "Creature",
                             "mana_cost": "{1}", "oracle_text": "", "cmc": 1.0},
    )


def test_deck_health_exposes_consistency_targets_tile(monkeypatch):
    import commander_builder.consistency_targets as ct
    import commander_builder.deck_health as dh

    _stub_scryfall(monkeypatch)
    seen = {}

    def fake_eval(deck_text, *, consistency=None, **kw):
        seen["consistency"] = consistency
        return {"checks": {}, "met": 0, "evaluated": 0, "lookup_failures": 0}

    monkeypatch.setattr(ct, "evaluate_consistency_targets", fake_eval)
    monkeypatch.setattr(dh, "consistency_signal", lambda t: {"p_3_lands_by_t3": 0.9})
    out = dh.compute_deck_health("[Main]\n1 Forest\n")
    assert out["consistency_targets"] == {
        "checks": {}, "met": 0, "evaluated": 0, "lookup_failures": 0}
    # the projection computed once is the one handed to the evaluator
    assert seen["consistency"] == {"p_3_lands_by_t3": 0.9}


def test_deck_health_targets_signal_degrades_to_none(monkeypatch):
    import commander_builder.consistency_targets as ct
    import commander_builder.deck_health as dh

    _stub_scryfall(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("outage")

    monkeypatch.setattr(ct, "evaluate_consistency_targets", boom)
    out = dh.compute_deck_health("[Main]\n1 Forest\n")
    assert out["consistency_targets"] is None
