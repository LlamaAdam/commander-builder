"""Tests for infinite-combo detection (F3)."""
from __future__ import annotations

import json

import pytest

from commander_builder import combo_detection
from commander_builder.combo_detection import (
    assess_deck_brackets, combo_bracket_floor, detect_combos_in_deck,
    is_game_ending, load_combos, refresh_combos,
)


def _deck(*cards: str) -> str:
    body = "[metadata]\nName=T\n[Commander]\n1 Atraxa, Praetors' Voice\n[Main]\n"
    body += "".join(f"1 {c}\n" for c in cards)
    return body


def test_detects_combo_when_all_cards_present():
    deck = _deck("Thassa's Oracle", "Demonic Consultation", "Sol Ring")
    found = detect_combos_in_deck(deck, combos=combo_detection._FALLBACK)
    keys = [tuple(c["cards"]) for c in found]
    assert ("Thassa's Oracle", "Demonic Consultation") in keys


def test_no_combo_when_a_piece_is_missing():
    deck = _deck("Thassa's Oracle", "Sol Ring")  # missing Consultation
    found = detect_combos_in_deck(deck, combos=combo_detection._FALLBACK)
    assert all(set(c["cards"]) - {"Thassa's Oracle"} for c in found)
    assert not any(set(c["cards"]) <= {"thassa's oracle", "sol ring"} for c in found)


def test_detection_is_case_insensitive():
    deck = _deck("KIKI-JIKI, MIRROR BREAKER", "restoration angel")
    found = detect_combos_in_deck(deck, combos=combo_detection._FALLBACK)
    assert any(set(c["cards"]) == {"Kiki-Jiki, Mirror Breaker", "Restoration Angel"}
               for c in found)


def test_results_sorted_by_popularity():
    combos = [
        {"cards": ["A", "B"], "produces": "x", "popularity": 5},
        {"cards": ["C", "D"], "produces": "y", "popularity": 99},
    ]
    deck = _deck("A", "B", "C", "D")
    found = detect_combos_in_deck(deck, combos=combos)
    assert [c["popularity"] for c in found] == [99, 5]


def test_refresh_writes_compact_db(tmp_path):
    page1 = json.dumps({"results": [
        {"uses": [{"card": {"name": "Hullbreaker Horror"}}, {"card": {"name": "Sol Ring"}}],
         "produces": [{"feature": {"name": "Infinite colorless mana"}}],
         "popularity": 314670, "identity": "U"},
        {"uses": [{"card": {"name": "Solo Card"}}],  # <2 cards → skipped
         "produces": [], "popularity": 10},
    ], "next": "PAGE2"})
    page2 = json.dumps({"results": [
        {"uses": [{"card": {"name": "Kiki-Jiki, Mirror Breaker"}},
                  {"card": {"name": "Restoration Angel"}}],
         "produces": [{"feature": {"name": "Infinite creatures"}}],
         "popularity": 50000, "identity": "R/W"},
    ], "next": None})
    pages = {"PAGE2": page2}

    def fake_opener(url):
        return pages.get(url, page1)

    out = tmp_path / "combos.json"
    n = refresh_combos(top_n=100, page_size=2, out_path=out, _opener=fake_opener)
    assert n == 2  # the 1-card variant was skipped
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["count"] == 2
    assert data["combos"][0]["cards"] == ["Hullbreaker Horror", "Sol Ring"]
    assert data["combos"][0]["produces"] == "Infinite colorless mana"


def test_load_combos_prefers_written_db(tmp_path, monkeypatch):
    out = tmp_path / "combos.json"
    out.write_text(json.dumps({"combos": [{"cards": ["X", "Y"], "produces": "z"}]}),
                   encoding="utf-8")
    monkeypatch.setattr(combo_detection, "COMBO_DATA_PATH", out)
    combos = load_combos()
    assert combos == [{"cards": ["X", "Y"], "produces": "z"}]


def test_load_combos_falls_back_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(combo_detection, "COMBO_DATA_PATH", tmp_path / "nope.json")
    assert load_combos() == combo_detection._FALLBACK


# --------------------------------------------------------------------------- #
# bracket awareness
# --------------------------------------------------------------------------- #
def test_is_game_ending_matches_win_and_infinite():
    assert is_game_ending({"produces": "Win the game"})
    assert is_game_ending({"produces": "Infinite colorless mana"})
    assert not is_game_ending({"produces": "Card advantage"})


def test_combo_bracket_floor_two_card_infinite_is_b4():
    combo = {"cards": ["A", "B"], "produces": "Win the game"}
    assert combo_bracket_floor(combo) == 4


def test_combo_bracket_floor_three_card_infinite_is_b3():
    combo = {"cards": ["A", "B", "C"], "produces": "Win the game"}
    assert combo_bracket_floor(combo) == 3


def test_combo_bracket_floor_value_combo_is_b1():
    combo = {"cards": ["A", "B"], "produces": "Draw two cards"}
    assert combo_bracket_floor(combo) == 1


def test_assess_flags_two_card_combo_as_violation_below_b4():
    # Thassa's Oracle + Demonic Consultation = two-card win -> floor B4.
    deck = _deck("Thassa's Oracle", "Demonic Consultation")
    res = assess_deck_brackets(deck, bracket=2, combos=combo_detection._FALLBACK)
    assert res["recommended_bracket"] == 4
    assert not res["within_bracket"]
    assert any("Thassa's Oracle" in c["cards"] for c in res["violations"])
    # every detected combo is annotated with its floor + game_ending flag
    assert all("bracket_floor" in c and "game_ending" in c for c in res["combos"])


def test_assess_within_bracket_when_target_meets_floor():
    deck = _deck("Thassa's Oracle", "Demonic Consultation")
    res = assess_deck_brackets(deck, bracket=4, combos=combo_detection._FALLBACK)
    assert res["within_bracket"] and res["violations"] == []
    assert res["recommended_bracket"] == 4


def test_assess_clean_deck_has_no_pressure():
    deck = _deck("Sol Ring", "Llanowar Elves")
    res = assess_deck_brackets(deck, bracket=1, combos=combo_detection._FALLBACK)
    assert res["combos"] == [] and res["recommended_bracket"] == 1
    assert res["within_bracket"]
