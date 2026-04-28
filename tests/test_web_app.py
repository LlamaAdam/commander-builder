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


def test_root_loads_static_assets(client):
    """Smoke test: root HTML references app.js and app.css, both
    served by Flask's static endpoint."""
    resp = client.get("/")
    body = resp.data.decode("utf-8")
    assert "app.js" in body
    assert "app.css" in body


def test_static_css_serves(client):
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert resp.mimetype == "text/css"
    assert b"--bg" in resp.data  # CSS variable from the theme


def test_static_js_serves(client):
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.mimetype.lower()
    assert b"renderDashboard" in resp.data


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


# ---------------------------------------------------------------------------
# /api/iterations
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_client(deck_dir, tmp_path, monkeypatch):
    """A client with a knowledge_log seeded by the demo script."""
    import importlib.util
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "seed_demo_knowledge_log.py"
    )
    spec = importlib.util.spec_from_file_location("_seed_demo_klog", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = tmp_path / "klog.sqlite"
    mod.seed_demo(db, deck_id="omnath")

    # Stub Scryfall (in case any dashboard call piggy-backs).
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )

    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    return app.test_client()


def test_iterations_endpoint_without_deck_returns_recent(seeded_client):
    resp = seeded_client.get("/api/iterations")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "iterations" in body
    assert body["count"] == 4
    # Recent-first ordering — newest is the v4 tutor experiment.
    assert "tutor" in body["iterations"][0]["deck_name"].lower()


def test_iterations_endpoint_filters_by_deck(seeded_client):
    resp = seeded_client.get("/api/iterations?deck=omnath")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 4
    assert body["deck_id"] == "omnath"
    # iterations_for_deck returns oldest-first.
    assert "v1" in body["iterations"][0]["deck_name"]


def test_iterations_endpoint_filters_unknown_deck(seeded_client):
    resp = seeded_client.get("/api/iterations?deck=does-not-exist")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 0
    assert body["iterations"] == []


def test_iterations_drops_deck_snapshot_blob(seeded_client):
    """Snapshot blobs would balloon JSON payloads — we omit them
    from the listing endpoint."""
    resp = seeded_client.get("/api/iterations?deck=omnath")
    body = resp.get_json()
    for entry in body["iterations"]:
        assert "deck_snapshot" not in entry


def test_iterations_400_on_bad_limit(seeded_client):
    resp = seeded_client.get("/api/iterations?limit=abc")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/dashboard?advise=1 + /api/advise
# ---------------------------------------------------------------------------

def _stub_advise(monkeypatch):
    """Stub improvement_advisor.advise so tests run offline."""
    from types import SimpleNamespace

    def fake(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="Mana on landfall",
                    evidence={"inclusion_pct": 78.0, "synergy_pct": 12.0,
                              "price_usd": 8.5},
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="Replaced by faster ramp",
                    evidence={},
                ),
                SimpleNamespace(
                    card="Tireless Tracker", action="add",
                    reason="Landfall payoff",
                    evidence={"inclusion_pct": 65.0, "synergy_pct": 18.0,
                              "price_usd": 4.0},
                ),
            ],
        )
    monkeypatch.setattr("commander_builder.improvement_advisor.advise", fake)


def test_dashboard_with_advise_param_includes_suggestions(client, monkeypatch):
    _stub_advise(monkeypatch)
    resp = client.get("/api/dashboard?deck=Alpha&advise=1")
    assert resp.status_code == 200
    body = resp.get_json()
    sugs = body["suggested_adds"]
    # Two adds (cuts excluded), no Cultivate.
    assert len(sugs) == 2
    cards = {s["card"] for s in sugs}
    assert "Lotus Cobra" in cards
    assert "Tireless Tracker" in cards
    assert "Cultivate" not in cards


def test_dashboard_without_advise_param_omits_suggestions(client, monkeypatch):
    """No `advise=1` → no advisor call, suggested_adds stays empty."""
    called: dict = {"n": 0}
    def boom(*a, **kw):
        called["n"] += 1
        raise RuntimeError("should not be called")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", boom,
    )
    resp = client.get("/api/dashboard?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["suggested_adds"] == []
    assert called["n"] == 0


def test_dashboard_advise_failure_degrades_gracefully(client, monkeypatch):
    """If advise() raises, dashboard still renders without suggestions."""
    def explode(*a, **kw):
        raise RuntimeError("EDHREC fetch failed")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", explode,
    )
    resp = client.get("/api/dashboard?deck=Alpha&advise=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["suggested_adds"] == []


def test_advise_endpoint_returns_suggestions(client, monkeypatch):
    _stub_advise(monkeypatch)
    resp = client.get("/api/advise?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["bracket"] == 3
    assert len(body["suggestions"]) == 2  # adds only
    first = body["suggestions"][0]
    assert {"card", "inclusion_pct", "synergy_pct",
            "rationale", "price_usd"} <= set(first.keys())


def test_advise_endpoint_503_on_failure(client, monkeypatch):
    def explode(*a, **kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", explode,
    )
    resp = client.get("/api/advise?deck=Alpha")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "advise unavailable"


def test_advise_endpoint_404_on_missing_deck(client):
    resp = client.get("/api/advise?deck=Ghost")
    assert resp.status_code == 404


def test_advise_endpoint_400_on_bad_bracket(client):
    resp = client.get("/api/advise?deck=Alpha&bracket=zzz")
    assert resp.status_code == 400
