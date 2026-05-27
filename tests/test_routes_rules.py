"""Tests for FP-007 slice 3 -- /api/rules/* combo + game-changers endpoints.

Covers:
- /api/rules/combo: returns all combos when no identity filter.
- /api/rules/combo?identity=...: filters to combos within that identity.
- /api/rules/combo: each combo has bracket_floor + game_ending fields.
- /api/rules/combo: identity filter is case-insensitive + order-agnostic.
- /api/rules/combo: unknown identity with no matching combos returns empty.
- /api/rules/game_changers: returns a sorted list with count + source.
- /api/rules/game_changers: Smothering Tithe in the fallback list.
"""
from __future__ import annotations

from pathlib import Path
import pytest

flask = pytest.importorskip("flask")

from commander_builder.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def deck_dir(tmp_path):
    d = tmp_path / "decks"
    d.mkdir()
    (d / "[USER] Stub [B3].dck").write_text(
        "[Commander]\n1 Krenko, Mob Boss\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def client(deck_dir):
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# A minimal fake combo list for deterministic tests.
_FAKE_COMBOS = [
    {
        "cards": ["Thassa's Oracle", "Demonic Consultation"],
        "produces": "Win the game",
        "identity": ["U", "B"],
        "popularity": 100,
    },
    {
        "cards": ["Devoted Druid", "Vizier of Remedies"],
        "produces": "Infinite green mana",
        "identity": ["W", "G"],
        "popularity": 50,
    },
    {
        "cards": ["Sanguine Bond", "Exquisite Blood"],
        "produces": "Infinite life drain",
        "identity": ["B"],
        "popularity": 30,
    },
]


# ---------------------------------------------------------------------------
# /api/rules/combo tests
# ---------------------------------------------------------------------------

def test_combo_returns_all_when_no_identity(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    body = client.get("/api/rules/combo").get_json()
    assert body["identity"] is None
    assert body["count"] == 3
    assert len(body["combos"]) == 3


def test_combo_identity_filter_ub(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    # UB should match the Thassa's Oracle combo (U+B) and Sanguine Bond (B only).
    body = client.get("/api/rules/combo?identity=UB").get_json()
    assert body["identity"] == "UB"
    cards_lists = [tuple(sorted(c["cards"])) for c in body["combos"]]
    assert tuple(sorted(["Thassa's Oracle", "Demonic Consultation"])) in cards_lists
    assert tuple(sorted(["Sanguine Bond", "Exquisite Blood"])) in cards_lists
    # W+G combo should NOT appear.
    assert tuple(sorted(["Devoted Druid", "Vizier of Remedies"])) not in cards_lists


def test_combo_identity_filter_case_insensitive(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    body_lower = client.get("/api/rules/combo?identity=ub").get_json()
    body_upper = client.get("/api/rules/combo?identity=UB").get_json()
    assert body_lower["count"] == body_upper["count"]


def test_combo_identity_unknown_returns_empty(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    # "R" only -- none of the fake combos have R in their identity.
    body = client.get("/api/rules/combo?identity=R").get_json()
    assert body["count"] == 0
    assert body["combos"] == []


def test_combo_result_has_bracket_floor_and_game_ending(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    body = client.get("/api/rules/combo").get_json()
    for c in body["combos"]:
        assert "bracket_floor" in c
        assert "game_ending" in c
        assert isinstance(c["bracket_floor"], int)
        assert isinstance(c["game_ending"], bool)


def test_combo_win_combo_has_bracket_floor_4(client, monkeypatch):
    """Two-card win combo should return bracket_floor=4."""
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    body = client.get("/api/rules/combo").get_json()
    thassa = next(
        (c for c in body["combos"] if "Thassa's Oracle" in c["cards"]), None
    )
    assert thassa is not None
    assert thassa["bracket_floor"] == 4
    assert thassa["game_ending"] is True


def test_combo_response_shape(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **kw: list(_FAKE_COMBOS),
    )
    body = client.get("/api/rules/combo").get_json()
    assert "identity" in body
    assert "combos" in body
    assert "count" in body
    assert isinstance(body["combos"], list)


# ---------------------------------------------------------------------------
# /api/rules/game_changers tests
# ---------------------------------------------------------------------------

def test_game_changers_returns_sorted_list(client):
    body = client.get("/api/rules/game_changers").get_json()
    assert "cards" in body
    assert "count" in body
    assert "source" in body
    assert isinstance(body["cards"], list)
    assert body["count"] == len(body["cards"])
    # Should be sorted.
    assert body["cards"] == sorted(body["cards"])


def test_game_changers_includes_known_card(client):
    body = client.get("/api/rules/game_changers").get_json()
    # Smothering Tithe is in the fallback list (see game_changers._FALLBACK).
    assert "Smothering Tithe" in body["cards"]


def test_game_changers_source_field(client):
    body = client.get("/api/rules/game_changers").get_json()
    assert body["source"] in ("cache", "fallback")


def test_game_changers_count_nonzero(client):
    body = client.get("/api/rules/game_changers").get_json()
    assert body["count"] > 0
