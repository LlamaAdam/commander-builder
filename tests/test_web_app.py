"""Tests for the FP-006 Flask scaffold.

Covers route shapes, deck enumeration, and path-traversal protection.
Mocks ``lookup_card`` so dashboard tests don't hit Scryfall.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app, _list_decks, _resolve_deck_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_deck(deck_dir: Path, name: str, commander: str = "Test Cmdr") -> Path:
    p = deck_dir / f"{name}.dck"
    body = (
        "[metadata]\n"
        f"Name={name}\n\n"
        "[Commander]\n"
        f"1 {commander}\n\n"
        "[Main]\n"
        + "1 Forest\n" * 35
        + "1 Cultivate\n" * 5
    )
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def deck_dir(tmp_path) -> Path:
    d = tmp_path / "decks"
    d.mkdir()
    _write_deck(d, "Alpha")
    _write_deck(d, "Bravo")
    return d


@pytest.fixture
def client(deck_dir, monkeypatch):
    """A Flask test client with Scryfall lookup stubbed."""
    def fake_lookup(name: str):
        if "Forest" in name:
            return {
                "type_line": "Basic Land — Forest",
                "oracle_text": "({T}: Add {G}.)",
                "cmc": 0.0,
                "color_identity": ["G"],
                "prices": {"usd": "0.05"},
            }
        if "Cultivate" in name:
            return {
                "type_line": "Sorcery",
                "oracle_text": "Search your library for up to two basic land cards...",
                "cmc": 3.0,
                "color_identity": ["G"],
                "prices": {"usd": "1.50"},
            }
        # Commander
        return {
            "type_line": "Legendary Creature — Elder Dragon",
            "oracle_text": "",
            "cmc": 5.0,
            "color_identity": ["G"],
            "prices": {"usd": "10.00"},
        }

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )

    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# _list_decks
# ---------------------------------------------------------------------------

def test_list_decks_finds_dck_files(deck_dir):
    decks = _list_decks(deck_dir)
    names = sorted(d["name"] for d in decks)
    assert names == ["Alpha", "Bravo"]


def test_list_decks_handles_missing_dir(tmp_path):
    assert _list_decks(tmp_path / "nope") == []


def test_list_decks_skips_non_dck(deck_dir):
    (deck_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    names = {d["name"] for d in _list_decks(deck_dir)}
    assert "notes" not in names


# ---------------------------------------------------------------------------
# _resolve_deck_path — traversal protection
# ---------------------------------------------------------------------------

def test_resolve_by_id_inside_dir(deck_dir):
    path = _resolve_deck_path(deck_dir, "Alpha", None)
    assert path is not None
    assert path.name == "Alpha.dck"


def test_resolve_by_id_missing_returns_none(deck_dir):
    assert _resolve_deck_path(deck_dir, "Ghost", None) is None


def test_resolve_explicit_path_outside_dir_blocked(deck_dir, tmp_path):
    outside = tmp_path / "outside.dck"
    outside.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    # Even though file exists, it's outside deck_dir → blocked.
    assert _resolve_deck_path(deck_dir, None, str(outside)) is None


def test_resolve_explicit_path_inside_dir_ok(deck_dir):
    path = _resolve_deck_path(deck_dir, None, str(deck_dir / "Alpha.dck"))
    assert path is not None
    assert path.name == "Alpha.dck"


def test_resolve_traversal_attempt_blocked(deck_dir):
    # ../.. attack
    sneaky = str(deck_dir / ".." / ".." / "etc" / "passwd")
    assert _resolve_deck_path(deck_dir, None, sneaky) is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_root_serves_placeholder_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Commander Builder" in resp.data
    assert b"<html" in resp.data.lower()


def test_health_reports_ok_and_deck_count(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["deck_count"] == 2


def test_decks_endpoint_lists_available(client):
    resp = client.get("/api/decks")
    assert resp.status_code == 200
    body = resp.get_json()
    names = sorted(d["name"] for d in body["decks"])
    assert names == ["Alpha", "Bravo"]


def test_dashboard_returns_data_for_known_deck(client):
    resp = client.get("/api/dashboard?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    # All seven panels present.
    for key in (
        "commander", "deck_progress", "stat_tiles",
        "mana_curve", "categories", "theme_tags", "suggested_adds",
    ):
        assert key in body, f"missing panel: {key}"
    # Commander parsed.
    assert body["commander"]["name"] == "Test Cmdr"
    # Lands counted.
    assert body["stat_tiles"]["lands"] >= 35


def test_dashboard_404_on_missing_deck(client):
    resp = client.get("/api/dashboard?deck=Ghost")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "deck not found"


def test_dashboard_400_on_bad_bracket(client):
    resp = client.get("/api/dashboard?deck=Alpha&bracket=zzz")
    assert resp.status_code == 400


def test_dashboard_with_valid_bracket(client):
    resp = client.get("/api/dashboard?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # Power level should be in 1..10.
    assert 1 <= body["stat_tiles"]["power_level"] <= 10


def test_dashboard_traversal_blocked(client, tmp_path):
    outside = tmp_path / "evil.dck"
    outside.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    resp = client.get(f"/api/dashboard?path={outside}")
    assert resp.status_code == 404
