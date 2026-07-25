"""Tests for infinite-combo detection (F3)."""
from __future__ import annotations

import json

import pytest

from commander_builder import combo_detection
from commander_builder.combo_detection import (
    assess_deck_brackets, combo_bracket_floor, detect_combos_in_deck,
    is_game_ending, load_combos, one_piece_away, refresh_combos,
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


# --------------------------------------------------------------------------- #
# Combo SPEED, not card count (WotC B3: no *early-game* two-card infinites)
#
# The floor used to be 4 for every game-ending combo of <= 2 cards. That is a
# stricter rule than the real one: B3 "Upgraded" explicitly ALLOWS late-game
# two-card infinites, so the old behavior slammed ordinary upgraded decks to
# B4. Speed is proxied by the combo's combined mana value.
# --------------------------------------------------------------------------- #

def _mv_lookup(**mvs):
    """Build a Scryfall-shaped lookup over ``name -> cmc``. Unlisted names
    resolve to None (the cold-cache / unknown-card case)."""
    table = {k.replace("_", " "): v for k, v in mvs.items()}
    return lambda name: ({"cmc": table[name]} if name in table else None)


def test_cheap_two_card_combo_still_floors_4():
    """A 3-mana pair IS assemblable in the early game — the exact case the
    WotC rule restricts. Floor stays 4."""
    combo = {"cards": ["Thassa's Oracle", "Demonic Consultation"],
             "produces": "Win the game"}
    lookup = _mv_lookup(**{"Thassa's_Oracle": 2.0, "Demonic_Consultation": 1.0})
    assert combo_bracket_floor(combo, lookup=lookup) == 4


def test_expensive_two_card_combo_floors_3_not_4():
    """Mikaeus + Triskelion is 12 total mana — you are not doing that early,
    and B3 permits late-game two-card infinites. Floor 3."""
    combo = {"cards": ["Mikaeus, the Unhallowed", "Triskelion"],
             "produces": "Infinite damage"}
    lookup = _mv_lookup(**{"Mikaeus,_the_Unhallowed": 6.0, "Triskelion": 6.0})
    assert combo_bracket_floor(combo, lookup=lookup) == 3


def test_late_game_threshold_is_inclusive():
    """Pin the documented constant's boundary: >= _LATE_GAME_COMBO_MV is
    late, one below it is early."""
    from commander_builder.combo_detection import _LATE_GAME_COMBO_MV
    at = {"cards": ["A", "B"], "produces": "Win the game"}
    assert combo_bracket_floor(
        at, lookup=_mv_lookup(A=_LATE_GAME_COMBO_MV - 1, B=1)) == 3
    assert combo_bracket_floor(
        at, lookup=_mv_lookup(A=_LATE_GAME_COMBO_MV - 2, B=1)) == 4


def test_unresolvable_combo_keeps_the_strict_floor_of_4():
    """Conservative on missing data. Over-flagging a late combo costs a deck
    one bracket of headroom; under-flagging an early one silently passes a
    deck that breaks the B3 rule."""
    combo = {"cards": ["A", "B"], "produces": "Win the game"}
    assert combo_bracket_floor(combo, lookup=lambda n: None) == 4


def test_partially_resolvable_combo_keeps_the_strict_floor_of_4():
    """A partial sum UNDERSTATES the cost, which would push an expensive
    combo below the threshold and produce the strict floor for the wrong
    reason. All-or-nothing: unknown is its own answer."""
    combo = {"cards": ["A", "B"], "produces": "Win the game"}
    # A alone already clears the late-game bar; B is unknown.
    assert combo_bracket_floor(combo, lookup=_mv_lookup(A=9)) == 4


def test_lookup_raising_keeps_the_strict_floor_of_4():
    """An injected lookup that blows up is missing data, not a crash."""
    def _boom(_name):
        raise RuntimeError("scryfall exploded")
    combo = {"cards": ["A", "B"], "produces": "Win the game"}
    assert combo_bracket_floor(combo, lookup=_boom) == 4


def test_combo_bracket_floor_is_backward_compatible_single_arg():
    """One-argument calls (bracket_estimator, assess_deck_brackets, and the
    pre-existing tests above) must keep working, and with no populated
    Scryfall cache they must keep returning the strict floor."""
    combo = {"cards": ["Nonexistent Card QQQ", "Nonexistent Card ZZZ"],
             "produces": "Win the game"}
    assert combo_bracket_floor(combo) == 4


def test_default_lookup_never_touches_the_network(monkeypatch):
    """The default resolver is CACHE-ONLY by contract: combo_bracket_floor
    runs per combo inside bracket_estimator, which runs per deck inside
    pool_curator loops and per iteration inside deck_builder's steer loop.
    A network round-trip in any of those is unacceptable."""
    from commander_builder import scryfall_client

    def _no_network(*a, **kw):
        raise AssertionError("combo_bracket_floor hit the network")

    def _no_fetching_lookup(*a, **kw):
        raise AssertionError("used the fetching lookup, not the cache reader")

    monkeypatch.setattr(scryfall_client.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(scryfall_client, "lookup_card", _no_fetching_lookup)
    combo = {"cards": ["Sol Ring", "Mana Crypt"], "produces": "Infinite mana"}
    assert combo_bracket_floor(combo) in (3, 4)


def test_cached_scryfall_reads_the_disk_snapshot(tmp_path, monkeypatch):
    """The cache-only reader resolves what ordinary lookup_card traffic has
    already persisted, and returns None (not a crash) for a cold entry."""
    from commander_builder import combo_detection as cd

    def _fake_cache_path(name):
        return tmp_path / f"{name.lower().replace(' ', '_')}.json"

    monkeypatch.setattr(
        "commander_builder.scryfall_client._cache_path", _fake_cache_path)
    (tmp_path / "warm_card.json").write_text(
        json.dumps({"cmc": 5.0}), encoding="utf-8")

    assert cd._cached_scryfall("Warm Card") == {"cmc": 5.0}
    assert cd._cached_scryfall("Cold Card") is None
    # A corrupt snapshot degrades to None rather than raising.
    (tmp_path / "bad_card.json").write_text("{not json", encoding="utf-8")
    assert cd._cached_scryfall("Bad Card") is None


def test_three_card_combo_floor_is_unaffected_by_speed():
    """3+ cards still floors at 3 on the setup heuristic — the MV rule only
    disambiguates the two-card case the WotC rule names."""
    cheap = {"cards": ["A", "B", "C"], "produces": "Win the game"}
    assert combo_bracket_floor(cheap, lookup=_mv_lookup(A=0, B=0, C=0)) == 3


def test_non_game_ending_combo_floor_is_1_without_any_lookup():
    """No bracket pressure means no reason to resolve mana values at all."""
    def _boom(_name):
        raise AssertionError("should not have resolved a value combo")
    combo = {"cards": ["A", "B"], "produces": "Draw two cards"}
    assert combo_bracket_floor(combo, lookup=_boom) == 1


# --------------------------------------------------------------------------- #
# one_piece_away — "am I ONE card short?" (the actionable question)
# --------------------------------------------------------------------------- #

_OPA_COMBOS = [
    {"cards": ["Thassa's Oracle", "Demonic Consultation"],
     "produces": "Win the game", "popularity": 100},
    {"cards": ["Kiki-Jiki, Mirror Breaker", "Restoration Angel"],
     "produces": "Infinite creatures", "popularity": 900},
    {"cards": ["Underworld Breach", "Lion's Eye Diamond", "Brain Freeze"],
     "produces": "Win the game", "popularity": 500},
    {"cards": ["Sanguine Bond", "Exquisite Blood"],
     "produces": "Infinite life drain", "popularity": 700},
]


def test_one_piece_away_finds_the_single_missing_card():
    deck = _deck("Thassa's Oracle", "Sol Ring")
    rows = one_piece_away(deck, combos=_OPA_COMBOS)
    assert [r["missing"] for r in rows] == ["Demonic Consultation"]
    row = rows[0]
    assert row["have"] == ["Thassa's Oracle"]
    assert row["cards"] == ["Thassa's Oracle", "Demonic Consultation"]
    assert row["produces"] == "Win the game"
    assert row["popularity"] == 100


def test_one_piece_away_ignores_combos_the_deck_already_has():
    """A complete combo is detect_combos_in_deck's job — reporting it here
    too would tell the user to add a card they already run."""
    deck = _deck("Thassa's Oracle", "Demonic Consultation")
    assert one_piece_away(deck, combos=_OPA_COMBOS) == []


def test_one_piece_away_ignores_two_away_combos():
    """Two missing pieces is a rebuild suggestion, not a card
    recommendation — the whole point of the cut is actionability."""
    deck = _deck("Underworld Breach", "Sol Ring")  # 2 of 3 pieces missing
    assert one_piece_away(deck, combos=_OPA_COMBOS) == []


def test_one_piece_away_handles_three_card_combos():
    """Exactly-one-missing works at any combo size."""
    deck = _deck("Underworld Breach", "Lion's Eye Diamond")
    rows = one_piece_away(deck, combos=_OPA_COMBOS)
    assert [r["missing"] for r in rows] == ["Brain Freeze"]
    assert rows[0]["have"] == ["Underworld Breach", "Lion's Eye Diamond"]


def test_one_piece_away_is_sorted_by_popularity_desc():
    """Feeds a card-scoring formula: the most-played line comes first."""
    deck = _deck("Thassa's Oracle", "Kiki-Jiki, Mirror Breaker",
                 "Sanguine Bond")
    rows = one_piece_away(deck, combos=_OPA_COMBOS)
    assert [r["popularity"] for r in rows] == [900, 700, 100]


def test_one_piece_away_carries_the_bracket_floor_completion_would_create():
    """So a B2 deck gets WARNED off the same row a B4 deck gets tempted by.
    Cheap pair -> 4 (early-game, B3-illegal); expensive pair -> 3."""
    deck = _deck("Thassa's Oracle", "Sanguine Bond")
    lookup = _mv_lookup(**{
        "Thassa's_Oracle": 2, "Demonic_Consultation": 1,   # 3 total -> early
        "Sanguine_Bond": 5, "Exquisite_Blood": 6,          # 11 total -> late
    })
    floors = {r["missing"]: r["bracket_floor"]
              for r in one_piece_away(deck, combos=_OPA_COMBOS, lookup=lookup)}
    assert floors["Demonic Consultation"] == 4
    assert floors["Exquisite Blood"] == 3


def test_one_piece_away_matching_is_case_insensitive():
    deck = _deck("THASSA'S ORACLE")
    rows = one_piece_away(deck, combos=_OPA_COMBOS)
    assert [r["missing"] for r in rows] == ["Demonic Consultation"]


def test_one_piece_away_skips_malformed_single_card_entries():
    """A 1-card "combo" is a data artifact; "one away" from it is just "you
    don't own a card", which is not a combo suggestion."""
    deck = _deck("Sol Ring")
    combos = [{"cards": ["Some Card"], "produces": "Win the game"},
              {"cards": [], "produces": "Win the game"}]
    assert one_piece_away(deck, combos=combos) == []


def test_one_piece_away_defaults_to_the_bundled_combo_db(tmp_path, monkeypatch):
    """Called with no explicit DB it uses load_combos, same as detection."""
    monkeypatch.setattr(combo_detection, "COMBO_DATA_PATH", tmp_path / "none.json")
    deck = _deck("Sanguine Bond", "Sol Ring")
    rows = one_piece_away(deck)
    assert any(r["missing"] == "Exquisite Blood" for r in rows)


def test_one_piece_away_on_an_empty_deck_is_empty():
    assert one_piece_away("", combos=_OPA_COMBOS) == []
