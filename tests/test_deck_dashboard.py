"""Tests for deck_dashboard.py — FP-006 backend prep.

Coverage:
- Expanded role taxonomy (land_payoff, win_condition).
- Price extraction from Scryfall card dicts.
- Power-level heuristic (bracket anchoring + game-changer count + cmc).
- Match-score combination of inclusion% + synergy% + rank bonus.
- Top-level build_dashboard end-to-end with mocked Scryfall lookups.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from commander_builder.deck_dashboard import (
    DISPLAY_CATEGORIES,
    DashboardData,
    _extract_price_usd,
    _power_level,
    _read_main_with_quantities,
    build_dashboard,
    classify_role_extended,
    match_score,
)


# ---------------------------------------------------------------------------
# Expanded role taxonomy
# ---------------------------------------------------------------------------

def test_classify_role_extended_landfall_payoff():
    role = classify_role_extended(
        "Landfall — Whenever a land enters under your control, this "
        "creature gets +2/+2 until end of turn.",
        "Creature — Beast",
    )
    assert role == "land_payoff"


def test_classify_role_extended_play_a_land_trigger():
    role = classify_role_extended(
        "Whenever you play a land, draw a card.",
        "Creature — Snake",
    )
    assert role == "land_payoff"


def test_classify_role_extended_win_condition():
    role = classify_role_extended(
        "Target opponent loses the game.",
        "Sorcery",
    )
    assert role == "win_condition"


def test_classify_role_extended_each_opponent_loses_life():
    role = classify_role_extended(
        "Each opponent loses 10 life.",
        "Creature — Beast",
    )
    assert role == "win_condition"


def test_classify_role_extended_falls_back_to_base_taxonomy():
    """When no land/win patterns match, fall through to staples.classify_role."""
    role = classify_role_extended(
        "Destroy target creature.",
        "Instant",
    )
    assert role == "removal"


def test_classify_role_extended_handles_empty():
    assert classify_role_extended("", "") == "other"


# ---------------------------------------------------------------------------
# _extract_price_usd
# ---------------------------------------------------------------------------

def test_extract_price_returns_float():
    data = {"prices": {"usd": "8.99", "eur": "7.50"}}
    assert _extract_price_usd(data) == 8.99


def test_extract_price_returns_none_when_missing():
    assert _extract_price_usd({"prices": {}}) is None
    assert _extract_price_usd({}) is None
    assert _extract_price_usd(None) is None


def test_extract_price_returns_none_for_unparseable():
    """Sometimes Scryfall returns null; ensure we don't crash."""
    assert _extract_price_usd({"prices": {"usd": None}}) is None


def test_extract_price_handles_zero():
    """Zero is a valid price (free promos)."""
    assert _extract_price_usd({"prices": {"usd": "0.05"}}) == 0.05


# ---------------------------------------------------------------------------
# _power_level
# ---------------------------------------------------------------------------

def test_power_level_low_for_high_cmc_no_game_changers():
    """Slow deck (avg cmc 4, 0 changers) below the casual midpoint."""
    p = _power_level(avg_cmc=4.5, n_game_changers=0, bracket=None)
    assert p <= 5


def test_power_level_high_for_fast_deck_with_changers():
    """Fast deck with multiple game-changers should be high."""
    p = _power_level(avg_cmc=2.2, n_game_changers=4, bracket=None)
    assert p >= 8


def test_power_level_anchors_to_bracket_when_specified():
    """Bracket 4 (high power) should pull score upward even with
    moderate metrics."""
    p_no_bracket = _power_level(avg_cmc=3.0, n_game_changers=1, bracket=None)
    p_bracket_4 = _power_level(avg_cmc=3.0, n_game_changers=1, bracket=4)
    assert p_bracket_4 > p_no_bracket


def test_power_level_clamped_to_1_to_10():
    """Even absurd inputs should produce a value in [1, 10]."""
    very_high = _power_level(avg_cmc=1.0, n_game_changers=20, bracket=5)
    very_low = _power_level(avg_cmc=8.0, n_game_changers=0, bracket=1)
    assert 1 <= very_high <= 10
    assert 1 <= very_low <= 10


def test_power_level_combo_archetype_nudges_up():
    p_combo = _power_level(avg_cmc=3.0, n_game_changers=1, bracket=None,
                           archetype="combo")
    p_other = _power_level(avg_cmc=3.0, n_game_changers=1, bracket=None,
                           archetype="midrange")
    assert p_combo >= p_other


# ---------------------------------------------------------------------------
# match_score
# ---------------------------------------------------------------------------

def test_match_score_inclusion_pct_is_baseline():
    """A card with 70% inclusion and no synergy should score ~70."""
    s = match_score(inclusion_pct=70.0, synergy_pct=0.0, rank_in_list=10)
    # No rank bonus at rank 10; just inclusion.
    assert s == 70


def test_match_score_synergy_adds_capped_bonus():
    """Synergy% adds a capped bonus (max 20pp)."""
    no_synergy = match_score(70.0, 0.0, rank_in_list=10)
    with_synergy = match_score(70.0, 30.0, rank_in_list=10)
    # Synergy 30% caps at +20 → 90.
    assert with_synergy - no_synergy <= 20


def test_match_score_rank_bonus_top_first():
    """Top of list gets a small bonus over rank 10."""
    top = match_score(60.0, 0.0, rank_in_list=0)
    bottom = match_score(60.0, 0.0, rank_in_list=10)
    assert top > bottom


def test_match_score_clamped_to_1_100():
    """Even extreme inputs must produce a value in [1, 100]."""
    very_high = match_score(99.0, 50.0, rank_in_list=0)
    very_low = match_score(0.0, 0.0, rank_in_list=10)
    assert 1 <= very_high <= 100
    assert 1 <= very_low <= 100


# ---------------------------------------------------------------------------
# _read_main_with_quantities
# ---------------------------------------------------------------------------

def test_read_main_parses_qty_name(tmp_path):
    deck = tmp_path / "test.dck"
    deck.write_text(
        "[metadata]\nMoxfield=abc\n[Commander]\n1 My Commander\n"
        "[Main]\n4 Forest\n2 Lightning Bolt\n1 Sol Ring\n",
        encoding="utf-8",
    )
    out = _read_main_with_quantities(deck)
    assert out == [("Forest", 4), ("Lightning Bolt", 2), ("Sol Ring", 1)]


def test_read_main_returns_empty_for_missing_file(tmp_path):
    assert _read_main_with_quantities(tmp_path / "missing.dck") == []


def test_read_main_skips_set_collector_suffix(tmp_path):
    """Lines with |SET|CN markers should still parse name correctly."""
    deck = tmp_path / "test.dck"
    deck.write_text(
        "[Main]\n1 Forest|MID|275\n",
        encoding="utf-8",
    )
    out = _read_main_with_quantities(deck)
    assert out == [("Forest", 1)]


# ---------------------------------------------------------------------------
# build_dashboard end-to-end (mocked Scryfall lookups)
# ---------------------------------------------------------------------------

def _write_simple_deck(tmp_path: Path, name: str = "test.dck") -> Path:
    """Synthesize a simple .dck file."""
    p = tmp_path / name
    p.write_text(
        "[metadata]\nMoxfield=test\n"
        "[Commander]\n1 Omnath, Locus of Creation\n"
        "[Main]\n"
        + ("1 Forest\n" * 37)
        + "1 Lotus Cobra\n"
        + "1 Cultivate\n"
        + "1 Wrath of God\n"
        + "1 Lightning Bolt\n",
        encoding="utf-8",
    )
    return p


def test_build_dashboard_returns_all_panels(tmp_path, monkeypatch):
    """All seven UI panels should be present in the result."""
    deck = _write_simple_deck(tmp_path)

    def fake_lookup(name):
        return {
            "Omnath, Locus of Creation": {
                "type_line": "Legendary Creature — Elemental Incarnation",
                "color_identity": ["W", "U", "R", "G"],
                "cmc": 4.0,
            },
            "Forest": {
                "type_line": "Basic Land — Forest",
                "oracle_text": "{T}: Add {G}.",
                "cmc": 0.0,
            },
            "Lotus Cobra": {
                "type_line": "Creature — Snake",
                "oracle_text": "Whenever a land enters the battlefield "
                               "under your control, add one mana of any "
                               "color.",
                "cmc": 2.0,
                "prices": {"usd": "8.00"},
            },
            "Cultivate": {
                "type_line": "Sorcery",
                "oracle_text": "Search your library for up to two basic "
                               "land cards.",
                "cmc": 3.0,
                "prices": {"usd": "0.50"},
            },
            "Wrath of God": {
                "type_line": "Sorcery",
                "oracle_text": "Destroy all creatures.",
                "cmc": 4.0,
                "prices": {"usd": "5.00"},
            },
            "Lightning Bolt": {
                "type_line": "Instant",
                "oracle_text": "Lightning Bolt deals 3 damage to any target.",
                "cmc": 1.0,
                "prices": {"usd": "1.50"},
            },
        }.get(name)

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", fake_lookup,
    )

    result = build_dashboard(deck, bracket=3)

    assert isinstance(result, DashboardData)
    # Commander panel.
    assert result.commander["name"] == "Omnath, Locus of Creation"
    # Deck progress.
    assert result.deck_progress["target"] == 100
    assert result.deck_progress["current"] >= 40
    # Stat tiles.
    assert result.stat_tiles["lands"] == 37  # 37 forests
    assert result.stat_tiles["est_price_usd"] == pytest.approx(15.0)
    # Mana curve has 0..6 buckets.
    curve_buckets = [b for b, _ in result.mana_curve]
    for b in range(7):
        assert b in curve_buckets
    # Categories slot for every display category.
    for cat in DISPLAY_CATEGORIES:
        assert cat in result.categories
    # We had Cultivate (ramp), Wrath of God (wipe), Lightning Bolt (removal),
    # Lotus Cobra (land_payoff trigger).
    assert result.categories["ramp"] >= 1
    assert result.categories["wipe"] >= 1
    assert result.categories["removal"] >= 1
    assert result.categories["land_payoff"] >= 1


def test_build_dashboard_with_suggestions_adds_match_pct(tmp_path, monkeypatch):
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: {"prices": {"usd": "3.00"}, "cmc": 0.0,
                   "type_line": "Land", "oracle_text": ""},
    )
    suggestions = [
        {"card": "Field of the Dead", "inclusion_pct": 80.0,
         "synergy_pct": 30.0, "rationale": "Landfall payoff"},
        {"card": "Scute Swarm", "inclusion_pct": 60.0,
         "synergy_pct": 50.0, "rationale": "Token landfall"},
    ]
    result = build_dashboard(deck, bracket=3, suggested=suggestions)
    assert len(result.suggested_adds) == 2
    field_dead = result.suggested_adds[0]
    assert field_dead["card"] == "Field of the Dead"
    assert 80 <= field_dead["match_pct"] <= 100
    assert field_dead["price_usd"] == 3.0
    assert "Landfall" in field_dead["rationale"]


def test_build_dashboard_to_dict_serializable(tmp_path, monkeypatch):
    """The DashboardData should round-trip through json.dumps cleanly
    so it can be served by the future Flask layer."""
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: None,
    )
    result = build_dashboard(deck, bracket=3)
    serialized = json.dumps(result.to_dict())
    assert "commander" in serialized
    assert "stat_tiles" in serialized
