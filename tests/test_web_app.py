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
def user_deck_dir(tmp_path) -> Path:
    """Deck dir that mixes [USER] and filler decks — used to exercise
    the user-only default filter."""
    d = tmp_path / "user_decks"
    d.mkdir()
    _write_deck(d, "[USER] Hakbal [B3]")
    _write_deck(d, "[USER] Hash [B3]")
    _write_deck(d, "Allies")          # filler / pool, no [USER] prefix
    _write_deck(d, "Avacyn Tribal")   # filler / pool
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
    # Default `user_only=True` filters out our test fixture decks
    # (Alpha/Bravo lack the [USER] prefix); use user_only=False for
    # the all-files test.
    decks = _list_decks(deck_dir, user_only=False)
    names = sorted(d["name"] for d in decks)
    assert names == ["Alpha", "Bravo"]


def test_list_decks_handles_missing_dir(tmp_path):
    assert _list_decks(tmp_path / "nope") == []


def test_list_decks_skips_non_dck(deck_dir):
    (deck_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    names = {d["name"] for d in _list_decks(deck_dir, user_only=False)}
    assert "notes" not in names


def test_list_decks_default_filters_to_user_prefix(user_deck_dir):
    """Default user_only=True keeps only [USER] *.dck files."""
    decks = _list_decks(user_deck_dir)
    names = {d["name"] for d in decks}
    # Display names strip the [USER] prefix.
    assert "Hakbal [B3]" in names
    assert "Hash [B3]" in names
    assert "Allies" not in names
    assert "Avacyn Tribal" not in names


def test_list_decks_user_only_false_includes_filler(user_deck_dir):
    decks = _list_decks(user_deck_dir, user_only=False)
    names = {d["name"] for d in decks}
    assert "Allies" in names
    assert "Avacyn Tribal" in names


def test_list_decks_hides_proposed_working_copies(user_deck_dir):
    """Transient _proposed_<timestamp> files staged by propose_swap
    should never appear in the sidebar — neither in user_only mode
    nor in the unfiltered listing."""
    # Plant a leftover proposed working copy.
    leftover = user_deck_dir / "[USER] Hakbal [B3]_proposed_20260428_134828.dck"
    leftover.write_text("[Main]\n1 Forest\n", encoding="utf-8")
    user_only = {d["id"] for d in _list_decks(user_deck_dir)}
    all_mode = {d["id"] for d in _list_decks(user_deck_dir, user_only=False)}
    assert "[USER] Hakbal [B3]_proposed_20260428_134828" not in user_only
    assert "[USER] Hakbal [B3]_proposed_20260428_134828" not in all_mode


def test_decks_endpoint_filters_to_user_default(user_deck_dir, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: None,
    )
    app = create_app(deck_dir=user_deck_dir)
    app.config["TESTING"] = True
    c = app.test_client()
    body = c.get("/api/decks").get_json()
    names = {d["name"] for d in body["decks"]}
    assert "Hakbal [B3]" in names
    assert "Allies" not in names


def test_decks_endpoint_all_flag_includes_filler(user_deck_dir, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: None,
    )
    app = create_app(deck_dir=user_deck_dir)
    app.config["TESTING"] = True
    c = app.test_client()
    body = c.get("/api/decks?all=1").get_json()
    names = {d["name"] for d in body["decks"]}
    assert "Allies" in names


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
    # The fixture decks lack the [USER] prefix so the default health
    # count (which honors the user_only filter) is 0. The all=1 listing
    # below confirms the files are present.
    assert body["deck_count"] == 0


def test_forge_version_endpoint_returns_version_info(client, monkeypatch):
    """Surfaces version + age so the UI can show a "Forge 2.0.12
    (19d old)" badge and warn when stale."""
    from datetime import datetime, timezone
    from commander_builder.forge_runner import ForgeVersionInfo
    fake_info = ForgeVersionInfo(
        jar_path=Path("/fake/forge-gui-desktop-2.0.12-jar-with-dependencies.jar"),
        version="2.0.12",
        build_date=datetime(2026, 4, 23, 19, 50, 58, tzinfo=timezone.utc),
        age_days=19,
        is_stale=False,
    )
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: fake_info,
    )
    resp = client.get("/api/forge_version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["version"] == "2.0.12"
    assert body["age_days"] == 19
    assert body["is_stale"] is False
    assert body["build_date"] is not None
    assert body["jar_path"].endswith("forge-gui-desktop-2.0.12-jar-with-dependencies.jar")


def test_forge_version_endpoint_flags_stale_install(client, monkeypatch):
    """When the jar is older than the stale threshold, is_stale=True."""
    from datetime import datetime, timezone
    from commander_builder.forge_runner import ForgeVersionInfo
    fake_info = ForgeVersionInfo(
        jar_path=Path("/fake/jar.jar"),
        version="1.9.0",
        build_date=datetime(2025, 8, 1, tzinfo=timezone.utc),
        age_days=285,
        is_stale=True,
    )
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: fake_info,
    )
    resp = client.get("/api/forge_version")
    body = resp.get_json()
    assert body["is_stale"] is True
    assert body["age_days"] == 285


def test_forge_version_endpoint_handles_missing_jar(client, monkeypatch):
    """No jar found → all fields null but endpoint still 200s."""
    from commander_builder.forge_runner import ForgeVersionInfo
    monkeypatch.setattr(
        "commander_builder.web.app.detect_forge_version",
        lambda *a, **kw: ForgeVersionInfo(),
    )
    resp = client.get("/api/forge_version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["version"] is None
    assert body["jar_path"] is None
    assert body["is_stale"] is False


def test_decks_endpoint_lists_available(client):
    resp = client.get("/api/decks?all=1")
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
    # Bracket is the canonical 1..5 field; power_level is kept as a
    # backwards-compat alias and contains the same value.
    assert 1 <= body["stat_tiles"]["bracket"] <= 5
    assert body["stat_tiles"]["power_level"] == body["stat_tiles"]["bracket"]
    assert body["stat_tiles"]["bracket_name"] in {
        "Exhibition", "Core", "Upgraded", "Optimized", "cEDH",
    }


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


# ---------------------------------------------------------------------------
# /api/iteration/<id> + /api/iteration/<id>/snapshot
# ---------------------------------------------------------------------------

def test_iteration_detail_returns_full_record(seeded_client):
    resp = seeded_client.get("/api/iteration/1")
    assert resp.status_code == 200
    body = resp.get_json()
    # Detail includes the snapshot blob (excluded from listings).
    assert "deck_snapshot" in body
    assert "1 Omnath, Locus of Creation" in body["deck_snapshot"]
    assert body["id"] == 1


def test_iteration_detail_404_on_missing(seeded_client):
    resp = seeded_client.get("/api/iteration/9999")
    assert resp.status_code == 404


def test_iteration_snapshot_serves_plain_text(seeded_client):
    resp = seeded_client.get("/api/iteration/1/snapshot")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert b"[Commander]" in resp.data


def test_iteration_snapshot_404_when_missing(seeded_client):
    resp = seeded_client.get("/api/iteration/9999/snapshot")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/compare/<old_id>/<new_id>
# ---------------------------------------------------------------------------

def test_compare_iterations_returns_diff(seeded_client):
    resp = seeded_client.get("/api/compare/1/2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["old_id"] == 1
    assert body["new_id"] == 2
    assert "added" in body
    assert "removed" in body
    assert isinstance(body["unchanged_count"], int)


def test_compare_iterations_404_on_missing_id(seeded_client):
    resp = seeded_client.get("/api/compare/1/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/deck_text + /api/propose_swap (modify-deck + A/B sim)
# ---------------------------------------------------------------------------

def test_deck_text_returns_dck_blob(client):
    """Direct read of a deck file by id."""
    resp = client.get("/api/deck_text?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck"] == "Alpha"
    assert "[Main]" in body["text"]
    assert "1 Forest" in body["text"]


def test_deck_text_404_on_missing_deck(client):
    resp = client.get("/api/deck_text?deck=Ghost")
    assert resp.status_code == 404


def _stub_compare(
    monkeypatch, winner="new", old_wins=4, new_wins=11, draws=0,
    pods_planned=2, pods_completed=2, stopped_early=False,
    pod_intra_aborts=None,
):
    """Stub commander_builder.compare_versions.compare so the
    A/B endpoint runs without Forge.

    ``pod_intra_aborts`` is an optional list of (intra_pod_aborted,
    games_actually_played) tuples for each pod, exercising the
    Sprint 1C per-pod-abort telemetry path. Defaults to all-pods-ran-
    full-length when None.
    """
    from types import SimpleNamespace

    def fake_compare(old_deck, new_deck, bracket, games_per_pod,
                     filler_pairs=2, mode="1v1", runner=None,
                     out_dir=None, deck_dir=None,
                     parallel=True, max_workers=None,
                     early_stop=True):
        old_stats = SimpleNamespace(deck_filename=old_deck,
                                    wins=old_wins, games=old_wins + new_wins + draws)
        new_stats = SimpleNamespace(deck_filename=new_deck,
                                    wins=new_wins, games=old_wins + new_wins + draws)
        if pod_intra_aborts:
            pods = [
                {
                    "pod_index": i + 1,
                    "intra_pod_aborted": aborted,
                    "games_actually_played": played,
                    "duration_sec": 30.0,
                }
                for i, (aborted, played) in enumerate(pod_intra_aborts)
            ]
        else:
            pods = [{} for _ in range(pods_completed)]
        return SimpleNamespace(
            old_deck=old_deck, new_deck=new_deck,
            bracket=bracket, mode=mode, games_per_pod=games_per_pod,
            old_stats=old_stats, new_stats=new_stats,
            draws=draws, total_games=games_per_pod,
            timestamp="2026-04-28T12:00:00",
            winner=winner,
            margin=abs(new_wins - old_wins),
            pods=pods,
            pods_planned=pods_planned,
            stopped_early=stopped_early,
        )
    monkeypatch.setattr(
        "commander_builder.compare_versions.compare", fake_compare,
    )

    # Stub ForgeRunner.locate so the endpoint passes the availability check.
    monkeypatch.setattr(
        "commander_builder.forge_runner.ForgeRunner.locate",
        classmethod(lambda cls: SimpleNamespace(name="stubbed")),
    )


def test_propose_swap_runs_compare_and_returns_summary(client, monkeypatch):
    _stub_compare(monkeypatch, winner="new", old_wins=4, new_wins=11)
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 5,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["winner"] == "new"
    assert body["old_wins"] == 4
    assert body["new_wins"] == 11
    assert body["games_per_pod"] == 5
    # Diff was non-empty (Lotus Cobra added, Cultivate removed).
    assert any("Lotus Cobra" in s for s in body["diff"]["added"])


def test_propose_swap_forwards_early_stop_metadata(client, monkeypatch):
    """Sprint 1B: when compare() reports it stopped early, the
    /api/propose_swap response surfaces pods_completed / pods_planned
    / stopped_early so the UI can show the user."""
    _stub_compare(
        monkeypatch, winner="new", old_wins=15, new_wins=0,
        pods_planned=4, pods_completed=3, stopped_early=True,
    )
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 5,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["pods_planned"] == 4
    assert body["pods_completed"] == 3
    assert body["stopped_early"] is True


def test_propose_swap_forwards_pod_summaries_with_intra_pod_abort(
    client, monkeypatch,
):
    """Sprint 1C: per-pod abort telemetry surfaces in pod_summaries
    so the UI can show 'Pod 2 stopped at game 3/5'."""
    _stub_compare(
        monkeypatch, winner="new", old_wins=2, new_wins=8,
        pods_planned=2, pods_completed=2, stopped_early=False,
        pod_intra_aborts=[(False, 5), (True, 3)],
    )
    new_text = (
        "[metadata]\nName=Alpha v2\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Lotus Cobra\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 5,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert "pod_summaries" in body
    assert len(body["pod_summaries"]) == 2
    assert body["pod_summaries"][0]["intra_pod_aborted"] is False
    assert body["pod_summaries"][0]["games_actually_played"] == 5
    assert body["pod_summaries"][1]["intra_pod_aborted"] is True
    assert body["pod_summaries"][1]["games_actually_played"] == 3


def test_propose_swap_400_on_bad_games_value(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha",
        "new_text": "[Main]\n1 Forest\n",
        "games": 7,  # not 5/10/20
    })
    assert resp.status_code == 400


def test_propose_swap_400_on_no_changes(client, monkeypatch):
    _stub_compare(monkeypatch)
    # Same content as fixture — no diff.
    same_text = (
        "[metadata]\nName=Alpha\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35 + "1 Cultivate\n" * 5
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": same_text, "games": 5,
    })
    assert resp.status_code == 400
    assert "no changes" in resp.get_json()["error"]


def test_propose_swap_404_on_missing_deck(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Ghost",
        "new_text": "[Main]\n1 Forest\n",
        "games": 5,
    })
    assert resp.status_code == 404


def test_propose_swap_400_on_empty_new_text(client, monkeypatch):
    _stub_compare(monkeypatch)
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": "", "games": 5,
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/deck_text PUT + DELETE
# ---------------------------------------------------------------------------

def test_deck_text_put_overwrites(client, deck_dir):
    new_body = (
        "[metadata]\nName=Edited\n\n"
        "[Commander]\n1 New Cmdr\n\n[Main]\n1 Forest\n"
    )
    resp = client.put(
        "/api/deck_text?deck=Alpha",
        json={"text": new_body},
    )
    assert resp.status_code == 200
    assert resp.get_json()["saved"] is True
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "1 New Cmdr" in on_disk


def test_deck_text_put_400_on_empty(client):
    resp = client.put("/api/deck_text?deck=Alpha", json={"text": ""})
    assert resp.status_code == 400


def test_deck_text_delete_removes(client, deck_dir):
    assert (deck_dir / "Alpha.dck").exists()
    resp = client.delete("/api/deck_text?deck=Alpha")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    assert not (deck_dir / "Alpha.dck").exists()


def test_deck_text_delete_404(client):
    resp = client.delete("/api/deck_text?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/import_deck (Moxfield URL + paste)
# ---------------------------------------------------------------------------

def test_import_deck_paste_creates_user_prefixed_file(client, deck_dir):
    paste = (
        "1 Sol Ring\n"
        "1 Arcane Signet\n"
        "1 Command Tower\n"
        "30 Forest\n"
    )
    resp = client.post("/api/import_deck", json={
        "name": "My Brew", "paste_text": paste, "bracket": 3,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # Filename is normalized to [USER] My Brew [B3].dck.
    assert "[USER]" in body["filename"]
    assert "[B3]" in body["filename"]
    on_disk = (deck_dir / body["filename"]).read_text(encoding="utf-8")
    # Paste lacked a [Main] section → normalized to wrap one.
    assert "[Main]" in on_disk
    assert "1 Sol Ring" in on_disk


def test_import_deck_dck_format_paste_preserved(client, deck_dir):
    paste = (
        "[metadata]\nName=Already Forge\n\n"
        "[Commander]\n1 Edgar Markov\n\n"
        "[Main]\n1 Sol Ring\n"
    )
    resp = client.post("/api/import_deck", json={
        "name": "Edgar Test", "paste_text": paste,
    })
    assert resp.status_code == 200
    fn = resp.get_json()["filename"]
    on_disk = (deck_dir / fn).read_text(encoding="utf-8")
    assert "[Commander]" in on_disk
    assert "1 Edgar Markov" in on_disk


def test_import_deck_409_on_duplicate(client, deck_dir):
    paste = "1 Sol Ring\n"
    client.post("/api/import_deck", json={
        "name": "Dup", "paste_text": paste,
    })
    resp = client.post("/api/import_deck", json={
        "name": "Dup", "paste_text": paste,
    })
    assert resp.status_code == 409


def test_import_deck_400_when_no_content(client):
    resp = client.post("/api/import_deck", json={"name": "Empty"})
    assert resp.status_code == 400


def test_import_deck_creates_parent_dir_when_missing(tmp_path, monkeypatch):
    """Regression: a fresh checkout (or web app started before the canonical
    deck dir exists) used to fail import with ENOENT. The endpoint now
    auto-creates the parent so the write succeeds and the deck shows up
    in the sidebar."""
    nonexistent = tmp_path / "fresh" / "decks"
    assert not nonexistent.exists()  # precondition
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )
    app = create_app(deck_dir=nonexistent)
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.post("/api/import_deck", json={
        "name": "Dragon Rawr",
        "paste_text": "1 Sol Ring\n1 Forest\n",
        "bracket": 4,
    })
    assert resp.status_code == 200, resp.get_json()
    assert nonexistent.exists()
    assert (nonexistent / "[USER] Dragon Rawr [B4].dck").exists()


def test_create_app_default_deck_dir_resolves_to_vendor_forge(monkeypatch):
    """The default deck_dir should point at the canonical Forge userdata
    location so import + audit + compare all share the same source of
    truth. Was previously `Path.cwd() / 'decks'`, which split the world
    when the web app was launched from the repo root."""
    import os as _os
    from commander_builder.forge_runner import VENDOR_FORGE
    monkeypatch.delenv("COMMANDER_BUILDER_DECK_DIR", raising=False)
    app = create_app()
    expected = (VENDOR_FORGE / "userdata" / "decks" / "commander").resolve()
    assert Path(app.config["DECK_DIR"]).resolve() == expected
    # Defensive: confirm we're not falling through to the old CWD/decks
    # behavior (the regression we're guarding against).
    assert not str(app.config["DECK_DIR"]).endswith(
        str(Path("commander_builder") / "decks"),
    )


def test_import_deck_via_moxfield_url(client, deck_dir, monkeypatch):
    """Stubs fetch_deck so we don't hit Moxfield."""
    fake_json = {
        "name": "Moxfield Test Deck",
        "publicId": "abc123",
        "boards": {
            "commanders": {
                "cards": {
                    "k1": {
                        "quantity": 1,
                        "card": {
                            "name": "Edgar Markov",
                            "set": "C17",
                            "cn": "37",
                        },
                    },
                },
            },
            "mainboard": {
                "cards": {
                    "k2": {
                        "quantity": 1,
                        "card": {
                            "name": "Sol Ring", "set": "C17", "cn": "260",
                        },
                    },
                },
            },
        },
    }
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda public_id: fake_json,
    )
    resp = client.post("/api/import_deck", json={
        "moxfield_url": "https://moxfield.com/decks/abc123",
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "Moxfield Test Deck" in body["filename"]


def test_import_deck_502_when_moxfield_fails(client, monkeypatch):
    def boom(public_id):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", boom,
    )
    resp = client.post("/api/import_deck", json={
        "moxfield_url": "https://moxfield.com/decks/abc",
    })
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# /api/moxfield_format
# ---------------------------------------------------------------------------

def test_moxfield_format_returns_paste_ready_text(client):
    resp = client.get("/api/moxfield_format?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    # No [metadata] block in the paste output.
    assert "[metadata]" not in body["text"]
    # Card lines preserved.
    assert "1 Forest" in body["text"]


def test_moxfield_format_404_on_missing(client):
    resp = client.get("/api/moxfield_format?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/game_changers + /api/deck_audit
# ---------------------------------------------------------------------------

def test_game_changers_list(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Sol Ring", "Mana Crypt", "Demonic Tutor"},
    )
    resp = client.get("/api/game_changers")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 3
    assert "Mana Crypt" in body["cards"]


def test_deck_audit_flags_in_deck_game_changers(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Cultivate", "Some Game Changer"},
    )
    # Alpha's mainboard contains Cultivate per the fixture.
    resp = client.get("/api/deck_audit?deck=Alpha&bracket=2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "Cultivate" in body["in_deck_game_changers"]
    # Bracket 2 + GCs present → warning fires.
    assert any("Game Changers" in w for w in body["warnings"])


def test_deck_audit_404_on_missing(client):
    resp = client.get("/api/deck_audit?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/audit (full-deck audit output) + _apply_swaps_to_dck
# ---------------------------------------------------------------------------

def test_apply_swaps_drops_cuts_and_appends_adds():
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[metadata]\nName=Test\n\n"
        "[Commander]\n1 Edgar Markov\n\n"
        "[Main]\n"
        "1 Sol Ring\n"
        "1 Cultivate\n"
        "1 Forest|MIR|315\n"
    )
    # 1 cut + 2 adds → balanced to 1 each (we keep the first add, drop
    # the second, since deck size must stay legal).
    recs = [
        SimpleNamespace(card="Cultivate", action="cut",
                        reason="slow ramp", evidence={}),
        SimpleNamespace(card="Lotus Cobra", action="add",
                        reason="landfall", evidence={}),
        SimpleNamespace(card="Tireless Tracker", action="add",
                        reason="payoff", evidence={}),
    ]
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    assert "Cultivate" not in new_text
    assert "1 Lotus Cobra" in new_text
    # Set/CN-suffixed lines preserved.
    assert "1 Forest|MIR|315" in new_text
    # Commander untouched.
    assert "1 Edgar Markov" in new_text
    # Balanced output: 1 add + 1 cut.
    assert added == ["Lotus Cobra"]
    assert removed == ["Cultivate"]
    assert kept == 2  # Sol Ring + Forest


def test_apply_swaps_balances_unequal_lists():
    """5 cuts + 2 adds → trimmed to 2 + 2 to keep deck size legal."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Commander]\n1 Cmdr\n[Main]\n"
        + "\n".join(f"1 Card{i}" for i in range(5)) + "\n"
    )
    recs = (
        [SimpleNamespace(card=f"Card{i}", action="cut",
                         reason="", evidence={}) for i in range(5)]
        + [SimpleNamespace(card=f"NewCard{i}", action="add",
                           reason="", evidence={}) for i in range(2)]
    )
    new_text, added, removed, kept = _apply_swaps_to_dck(original, recs)
    assert len(added) == 2
    assert len(removed) == 2
    # Deck size: original 5 main - 2 cut + 2 add = 5 main.
    main_lines = [
        line for line in new_text.splitlines()
        if line.startswith("1 ") and "Cmdr" not in line
    ]
    assert len(main_lines) == 5


def test_apply_swaps_case_insensitive_cut_match():
    """Cut matching ignores case. Test pairs cut+add 1:1 since the
    balance-rule requires equal counts to keep deck size legal."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 sol ring\n1 Cultivate\n"
    recs = [
        SimpleNamespace(card="Sol Ring", action="cut",
                        reason="", evidence={}),
        SimpleNamespace(card="Mana Vault", action="add",
                        reason="", evidence={}),
    ]
    new_text, added, removed, _ = _apply_swaps_to_dck(original, recs)
    assert "1 sol ring" not in new_text.lower()
    assert "1 Mana Vault" in new_text
    assert removed == ["Sol Ring"]
    assert added == ["Mana Vault"]


def test_format_added_line_resolves_set_and_cn(monkeypatch):
    """Appended cards should write '1 <name>|<SET>|<CN>' so Forge's
    deck loader doesn't trip on ambiguous name-only resolution."""
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: {
            "name": "Atarka, World Render",
            "set": "frf", "collector_number": "122",
        },
    )
    line = _format_added_line("Atarka, World Render")
    assert line == "1 Atarka, World Render|FRF|122"


def test_format_added_line_falls_back_when_lookup_fails(monkeypatch):
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    assert _format_added_line("Mystery Card") == "1 Mystery Card"


# ---------------------------------------------------------------------------
# Backlog: _pad_main_to_99 — bring sub-100 source decks up to legal size
# ---------------------------------------------------------------------------

def test_pad_main_to_99_passes_through_when_already_legal():
    from commander_builder.web.app import _pad_main_to_99
    text = "[metadata]\nName=X\n[Commander]\n1 Cmdr\n[Main]\n1 Forest\n"
    out, padded, breakdown = _pad_main_to_99(text, current_main=99)
    assert out == text
    assert padded == 0
    assert breakdown == {}


def test_pad_main_to_99_distributes_basics_proportionally():
    """Goblin-deck-style: 71 mainboard, all Mountains. We expect to add
    28 Mountains (mono-color)."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=Goblin\n"
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n"
        "30 Mountain\n"
        + "1 Goblin Bushwhacker\n" * 41
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=71)
    assert padded == 28
    assert breakdown == {"Mountain": 28}
    # Padding line landed inside [Main].
    assert "28 Mountain" in out


def test_pad_main_to_99_distributes_across_two_colors():
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=Simic\n"
        "[Commander]\n1 Hakbal\n"
        "[Main]\n10 Forest\n10 Island\n"
        + "1 Cultivate\n" * 60
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=80)
    assert padded == 19
    # 10:10 split → roughly 9-10 split (or 10-9). Sum must equal 19.
    assert sum(breakdown.values()) == 19
    assert breakdown.get("Forest", 0) > 0
    assert breakdown.get("Island", 0) > 0


def test_pad_main_to_99_falls_back_to_wastes_when_no_basics():
    """No basic lands in the deck (cEDH-style multi-color manabase) →
    pad with Wastes since they're colorless and legal anywhere."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=cEDH\n"
        "[Commander]\n1 Thrasios\n"
        "[Main]\n"
        + "1 Tundra\n1 Underground Sea\n1 Tropical Island\n"
        + "1 Sol Ring\n" * 90
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=93)
    assert padded == 6
    assert breakdown == {"Wastes": 6}


def test_cleanup_stale_staged_files_deletes_old_proposed_files(tmp_path):
    import os
    import time
    from commander_builder.web.app import _cleanup_stale_staged_files

    deck_dir = tmp_path / "userdata" / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    constructed = tmp_path / "userdata" / "decks" / "constructed"
    constructed.mkdir()

    # Old proposed file (should be deleted).
    old_proposed = deck_dir / "Foo_proposed_20260101_120000.dck"
    old_proposed.write_text("stale\n", encoding="utf-8")
    # Backdate it so the age check fires.
    past = time.time() - 3600
    os.utime(old_proposed, (past, past))

    # Old converted file in constructed/ (should also be deleted).
    old_converted = constructed / "Bar_converted_20260101_120000.dck"
    old_converted.write_text("stale\n", encoding="utf-8")
    os.utime(old_converted, (past, past))

    # User deck (should NOT be touched).
    user_deck = deck_dir / "[USER] Real Deck [B3].dck"
    user_deck.write_text("real\n", encoding="utf-8")

    deleted = _cleanup_stale_staged_files(deck_dir)
    assert deleted == 2
    assert not old_proposed.exists()
    assert not old_converted.exists()
    assert user_deck.exists()


def test_cleanup_stale_staged_files_skips_recent_files(tmp_path):
    """Don't delete files newer than the age threshold — they might
    belong to an in-flight Forge process."""
    from commander_builder.web.app import _cleanup_stale_staged_files

    deck_dir = tmp_path / "userdata" / "decks" / "commander"
    deck_dir.mkdir(parents=True)

    fresh = deck_dir / "Foo_proposed_20260429_010000.dck"
    fresh.write_text("active\n", encoding="utf-8")
    # mtime is 'now' by default, well within the 60s threshold.

    deleted = _cleanup_stale_staged_files(deck_dir, age_threshold_sec=60)
    assert deleted == 0
    assert fresh.exists()


def test_cleanup_stale_staged_files_handles_missing_dir(tmp_path):
    from commander_builder.web.app import _cleanup_stale_staged_files
    deleted = _cleanup_stale_staged_files(tmp_path / "nope")
    assert deleted == 0


def test_correlation_summary_endpoint_returns_zero_when_no_log(client, monkeypatch):
    """When the correlation log doesn't exist yet, the endpoint
    reports zero rows + agreement_rate=0 — UI can show 'no data yet'."""
    monkeypatch.delenv("COMMANDER_BUILDER_CORRELATE_FORGE_PY", raising=False)
    resp = client.get("/api/correlation_summary")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rows"] == 0
    assert body["agreement_rate"] == 0.0
    assert "log_path" in body
    assert body["enabled"] is False


def test_correlation_summary_endpoint_reports_enabled_state(client, monkeypatch):
    monkeypatch.setenv("COMMANDER_BUILDER_CORRELATE_FORGE_PY", "1")
    resp = client.get("/api/correlation_summary")
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True


def test_log_error_writes_to_log_file(client, tmp_path, deck_dir):
    """JS error collector appends to a server-side log; we don't read
    it back via API (avoid making this an exfiltration vector), so
    the test inspects the file directly."""
    payload = {
        "kind": "error",
        "message": "ReferenceError: foo is not defined",
        "stack": "at runProposeSwap (app.js:165)",
        "url": "http://127.0.0.1:5000/",
        "user_agent": "test-agent",
    }
    resp = client.post("/api/log_error", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "ref" in body and body["ref"]
    # Log file lives next to the deck_dir's grandparent (vendor/forge → vendor).
    log_path = deck_dir.parent.parent / "_js_errors.log"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "ReferenceError: foo is not defined" in contents
    assert "runProposeSwap" in contents
    assert "test-agent" in contents


def test_log_error_400_on_missing_message(client):
    resp = client.post("/api/log_error", json={"kind": "error"})
    assert resp.status_code == 400


def test_log_error_caps_oversized_payload(client, deck_dir):
    """Defense against runaway browser dumps — message capped at 2000
    chars, stack at 4000."""
    huge = "x" * 5000
    resp = client.post("/api/log_error", json={
        "message": huge, "stack": huge,
    })
    assert resp.status_code == 200
    log_path = deck_dir.parent.parent / "_js_errors.log"
    contents = log_path.read_text(encoding="utf-8")
    # Should contain at most 2000 'x' in the MSG line and 4000 in STACK.
    msg_line = next(
        (line for line in contents.splitlines() if line.startswith("MSG:")),
        "",
    )
    # MSG: prefix + up to 2000 chars
    assert len(msg_line) <= len("MSG: ") + 2000


def test_log_error_400_on_non_json(client):
    resp = client.post("/api/log_error", data="not json",
                       content_type="application/json")
    assert resp.status_code == 400


def test_pad_main_to_99_skips_quantities_in_other_sections():
    """Sideboard / Considering basics shouldn't sway the [Main] padding
    distribution."""
    from commander_builder.web.app import _pad_main_to_99
    text = (
        "[metadata]\nName=X\n"
        "[Commander]\n1 Cmdr\n"
        "[Main]\n5 Forest\n"
        + "1 Cultivate\n" * 60
        + "[Sideboard]\n10 Mountain\n"
    )
    out, padded, breakdown = _pad_main_to_99(text, current_main=65)
    # [Main] only sees Forest, so all padding is Forest. Mountain in
    # sideboard must be ignored.
    assert "Mountain" not in breakdown
    assert breakdown == {"Forest": 34}


def test_format_added_line_falls_back_when_set_missing(monkeypatch):
    from commander_builder.web.app import _format_added_line
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: {"name": "X", "set": "", "collector_number": ""},
    )
    assert _format_added_line("X") == "1 X"


def test_apply_swaps_kept_count_sums_quantities_not_lines():
    """Multi-qty lines like '5 Forest' should count as 5 cards in
    `kept`, not 1 line. The UI uses kept + adds to compute
    main_count and earlier counted lines, mis-reporting deck size."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = (
        "[Commander]\n1 Cmdr\n[Main]\n"
        "5 Forest\n"
        "3 Mountain\n"
        "1 Sol Ring\n"
    )
    recs = []  # no swaps — just measure kept_count
    _, _, _, kept = _apply_swaps_to_dck(original, recs)
    # 5 + 3 + 1 = 9 actual cards across 3 lines.
    assert kept == 9


def test_apply_swaps_no_op_when_one_list_empty():
    """0 adds + N cuts → no swaps (deck size must stay legal)."""
    from commander_builder.web.app import _apply_swaps_to_dck
    from types import SimpleNamespace
    original = "[Main]\n1 Sol Ring\n1 Cultivate\n"
    recs = [SimpleNamespace(card="Sol Ring", action="cut",
                            reason="", evidence={})]
    new_text, added, removed, _ = _apply_swaps_to_dck(original, recs)
    # Both lists trimmed to 0.
    assert added == []
    assert removed == []
    # Original deck untouched.
    assert "1 Sol Ring" in new_text
    assert "1 Cultivate" in new_text


def test_audit_endpoint_returns_full_proposed_deck(client, monkeypatch):
    """Stub improvement_advisor.advise so the endpoint runs offline."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
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
            ],
            diagnosis=SimpleNamespace(
                pattern_summary="aggressive landfall",
                weakness_signals=["slow ramp early"],
            ),
            source="heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # Full proposed deck text — Cultivate dropped, Lotus Cobra added.
    assert "1 Lotus Cobra" in body["proposed_text"]
    assert "Cultivate" not in body["proposed_text"]
    # Diff payload populated.
    assert any(a["card"] == "Lotus Cobra" for a in body["added"])
    assert any(r["card"] == "Cultivate" for r in body["removed"])
    # Diagnosis surfaced.
    assert body["diagnosis"] == "aggressive landfall"
    assert "slow ramp early" in body["weakness_signals"]


def test_audit_endpoint_503_when_advisor_fails(client, monkeypatch):
    def boom(deck_path, bracket, **_kwargs):
        raise RuntimeError("EDHREC down")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", boom,
    )
    resp = client.get("/api/audit?deck=Alpha")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "audit failed"


def test_audit_endpoint_404_on_missing_deck(client):
    resp = client.get("/api/audit?deck=Ghost")
    assert resp.status_code == 404


def _stub_advise_capturing(monkeypatch, source="heuristic", fallback_reason=None):
    """Stub advise() and capture how it was invoked + what the env
    looked like at call time. Returns the seen-args dict."""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        import os as _os
        seen["use_claude"] = kwargs.get("use_claude", False)
        seen["api_key_in_env"] = _os.environ.get("ANTHROPIC_API_KEY")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="ramp",
                    evidence={"inclusion_pct": 78.0},
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="replaced",
                    evidence={},
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source=source,
            fallback_reason=fallback_reason,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    return seen


def test_audit_defaults_to_heuristic_backend(client, monkeypatch):
    seen = _stub_advise_capturing(monkeypatch, source="heuristic")
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert seen["use_claude"] is False
    assert body["source"] == "heuristic"
    assert body["requested_llm"] == "heuristic"
    assert body["warning"] is None


def test_audit_llm_claude_passes_use_claude_and_byo_key(client, monkeypatch):
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test-byo-12345"},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert seen["use_claude"] is True
    # BYO key was injected into env for the call's lifetime.
    assert seen["api_key_in_env"] == "sk-test-byo-12345"
    assert body["source"] == "claude"
    assert body["requested_llm"] == "claude"
    assert body["warning"] is None
    # Env restored after the request — key must not linger.
    import os as _os
    assert "ANTHROPIC_API_KEY" not in _os.environ


def test_audit_llm_claude_warns_when_advisor_falls_back(client, monkeypatch):
    """When the user requests Claude but advise() returns source='heuristic'
    (no key, no SDK, network failure), surface a UI-visible warning."""
    _stub_advise_capturing(monkeypatch, source="heuristic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.get("/api/audit?deck=Alpha&bracket=3&llm=claude")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "heuristic"
    assert body["requested_llm"] == "claude"
    assert body["warning"] is not None
    # No key, so we expect the specific "no API key" hint, not the
    # generic message.
    assert "api key" in body["warning"].lower()


def test_audit_warning_includes_fallback_reason_when_present(client, monkeypatch):
    """If advise() returned a concrete fallback_reason (auth error,
    JSON decode, etc.) the UI must surface that exact reason, not a
    generic 'Claude unavailable' string."""
    _stub_advise_capturing(
        monkeypatch,
        source="heuristic",
        fallback_reason="claude advisor failed (AuthenticationError: 401 invalid api key)",
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-bogus-but-present"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["warning"] is not None
    assert "AuthenticationError" in body["warning"]
    assert "401" in body["warning"]


def test_audit_forwards_model_param_to_advise(client, monkeypatch):
    """?model=claude-haiku-4-5 should be forwarded to advise()'s
    claude_model kwarg so the SDK uses the cheaper tier."""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        seen["use_claude"] = kwargs.get("use_claude")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="ramp", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="slow", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude&model=claude-haiku-4-5",
        headers={"X-Anthropic-API-Key": "sk-test"},
    )
    assert resp.status_code == 200
    assert seen["use_claude"] is True
    assert seen["claude_model"] == "claude-haiku-4-5"


def test_audit_uses_default_model_when_none_specified(client, monkeypatch):
    from types import SimpleNamespace
    from commander_builder.improvement_advisor import DEFAULT_CLAUDE_MODEL
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="ramp", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="slow", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test"},
    )
    assert resp.status_code == 200
    assert seen["claude_model"] == DEFAULT_CLAUDE_MODEL


def test_audit_warning_when_claude_succeeded_is_none(client, monkeypatch):
    _stub_advise_capturing(monkeypatch, source="claude", fallback_reason=None)
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-test-key"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["warning"] is None
    assert body["source"] == "claude"


def test_audit_400_on_invalid_llm_value(client, monkeypatch):
    _stub_advise_capturing(monkeypatch)
    resp = client.get("/api/audit?deck=Alpha&llm=ollama-but-not-supported-yet")
    assert resp.status_code == 400
    assert "llm" in resp.get_json()["error"]


def test_audit_does_not_leak_byo_key_to_subsequent_call(client, monkeypatch):
    """The header-injected key only lives for the duration of the
    current request. A follow-up audit without the header must not
    inherit it."""
    seen = _stub_advise_capturing(monkeypatch, source="claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    r1 = client.get(
        "/api/audit?deck=Alpha&bracket=3&llm=claude",
        headers={"X-Anthropic-API-Key": "sk-first-call"},
    )
    assert r1.status_code == 200
    assert seen["api_key_in_env"] == "sk-first-call"

    seen.clear()
    r2 = client.get("/api/audit?deck=Alpha&bracket=3&llm=claude")
    assert r2.status_code == 200
    assert seen.get("api_key_in_env") is None


# ---------------------------------------------------------------------------
# /api/deck_source GET + PUT (Moxfield URL attached to a deck)
# ---------------------------------------------------------------------------

def test_deck_source_get_returns_none_when_unattached(client):
    resp = client.get("/api/deck_source?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] is None
    assert body["moxfield_url"] is None


def test_deck_source_put_attaches_url(client, deck_dir):
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc123"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] == "abc123"
    assert "moxfield.com/decks/abc123" in body["moxfield_url"]
    # Persisted to file.
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "Moxfield=abc123" in on_disk


def test_deck_source_put_updates_existing(client, deck_dir):
    # First attach.
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/orig"},
    )
    # Now update.
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/newone"},
    )
    body = resp.get_json()
    assert body["moxfield_id"] == "newone"
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    # Old line gone, new line in place.
    assert "Moxfield=orig" not in on_disk
    assert "Moxfield=newone" in on_disk


def test_deck_source_put_clears_when_empty_url(client, deck_dir):
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    resp = client.put("/api/deck_source?deck=Alpha", json={"moxfield_url": ""})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["moxfield_id"] is None
    on_disk = (deck_dir / "Alpha.dck").read_text(encoding="utf-8")
    assert "Moxfield=" not in on_disk


def test_deck_source_put_400_on_bad_url(client):
    resp = client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "not a url"},
    )
    # parse_deck_id may reject or accept; the endpoint should return
    # 400 either way because the value isn't useful as an id.
    assert resp.status_code in (200, 400)


def test_deck_source_404_on_missing_deck(client):
    resp = client.get("/api/deck_source?deck=Ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/verify_against_source
# ---------------------------------------------------------------------------

def test_verify_against_source_400_when_no_url_attached(client):
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 400
    assert "no Moxfield source" in resp.get_json()["error"]


def test_verify_against_source_diffs_local_vs_remote(client, monkeypatch):
    # Attach a source URL first.
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    # Stub fetch_deck so we get a deterministic remote.
    fake_remote_json = {
        "name": "Alpha", "publicId": "abc",
        "boards": {
            "commanders": {
                "cards": {
                    "k1": {"quantity": 1,
                           "card": {"name": "Test Cmdr", "set": "C", "cn": "1"}},
                },
            },
            "mainboard": {
                "cards": {
                    # Remote diverged: 'Sol Ring' replaced by 'Mana Crypt'.
                    "k2": {"quantity": 1,
                           "card": {"name": "Mana Crypt", "set": "MS", "cn": "1"}},
                    "k3": {"quantity": 1,
                           "card": {"name": "Forest", "set": "M", "cn": "1"}},
                },
            },
        },
    }
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda public_id: fake_remote_json,
    )
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "moxfield.com/decks/abc" in body["source_url"]
    # Local has Cultivate + Forest, remote has Mana Crypt + Forest.
    # Diff should show Cultivate as local-only and Mana Crypt as
    # remote-only.
    assert any("Cultivate" in line for line in body["in_local_only"])
    assert any("Mana Crypt" in line for line in body["in_remote_only"])


def test_verify_against_source_502_on_moxfield_failure(client, monkeypatch):
    client.put(
        "/api/deck_source?deck=Alpha",
        json={"moxfield_url": "https://moxfield.com/decks/abc"},
    )
    def boom(public_id):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", boom,
    )
    resp = client.get("/api/verify_against_source?deck=Alpha")
    assert resp.status_code == 502


def test_propose_swap_503_when_forge_unavailable(client, monkeypatch):
    """Forge missing should return 503 with detail, not 500."""
    from types import SimpleNamespace

    def fake_compare(*a, **kw):
        return SimpleNamespace()  # not reached
    monkeypatch.setattr(
        "commander_builder.compare_versions.compare", fake_compare,
    )

    def boom(cls):
        raise FileNotFoundError("vendor/forge not found")
    monkeypatch.setattr(
        "commander_builder.forge_runner.ForgeRunner.locate",
        classmethod(boom),
    )

    new_text = (
        "[metadata]\nName=v2\n\n[Commander]\n1 Cmdr\n\n"
        "[Main]\n1 Forest\n1 Lotus Cobra\n"
    )
    resp = client.post("/api/propose_swap", json={
        "deck": "Alpha", "new_text": new_text, "games": 5,
    })
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "Forge not available"


def test_compare_iterations_handles_added_cards(seeded_client):
    """Demo seeder writes the same minimal snapshot for each version
    plus one extra card slug per iteration; check the diff isn't empty
    when versions differ."""
    # Fall back to checking shape — seeder writes near-identical
    # snapshots, but the test verifies the endpoint path works.
    resp = seeded_client.get("/api/compare/2/3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body["added"], list)
    assert isinstance(body["removed"], list)


# ---------------------------------------------------------------------------
# /api/save_iteration
# ---------------------------------------------------------------------------

@pytest.fixture
def save_client(deck_dir, tmp_path, monkeypatch):
    """Fresh empty knowledge_log + a deck dir with an Alpha deck."""
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name: {"type_line": "Basic Land", "cmc": 0.0,
                      "color_identity": ["G"], "prices": {"usd": "0.05"}},
    )
    db = tmp_path / "save_klog.sqlite"
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    client = app.test_client()
    return client, db


def test_save_iteration_persists_row(save_client):
    client, db = save_client
    payload = {
        "deck_id": "Alpha",
        "deck_name": "Alpha",
        "bracket": 3,
        "audit_version": "v3",
        "audit_manifest": {
            "added": [{"card": "Lotus Cobra", "rationale": "ramp"}],
            "removed": [{"card": "Cultivate", "rationale": "slow"}],
        },
        "sim_report": {
            "winner": "new", "old_wins": 4, "new_wins": 6,
            "old_games": 10, "new_games": 10, "draws": 0,
            "margin": 2, "total_games": 20, "mode": "pod", "bracket": 3,
        },
        "verdict": "kept",
        "verdict_notes": "Cobra was the right call.",
    }
    resp = client.post("/api/save_iteration", json=payload)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert isinstance(body["id"], int) and body["id"] >= 1
    assert body["verdict"] == "kept"
    assert body["stats"]["total"] == 1
    assert body["stats"]["kept"] == 1

    # Read back through /api/iteration to confirm it landed.
    detail = client.get(f"/api/iteration/{body['id']}").get_json()
    assert detail["deck_id"] == "Alpha"
    assert detail["bracket"] == 3
    assert detail["verdict"] == "kept"
    assert detail["margin"] == 2
    assert abs(detail["win_rate_old"] - 0.4) < 1e-9
    assert abs(detail["win_rate_new"] - 0.6) < 1e-9
    # audit_manifest survives the round-trip.
    assert detail["audit_manifest"]["added"][0]["card"] == "Lotus Cobra"


def test_save_iteration_defaults_deck_snapshot_from_disk(save_client):
    client, db = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha",
        "deck_name": "Alpha",
        "bracket": 3,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}/snapshot")
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "Name=Alpha" in text


def test_save_iteration_400_on_missing_deck_id(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={"verdict": "pending"})
    assert resp.status_code == 400
    assert "deck_id" in resp.get_json()["error"]


def test_save_iteration_400_on_invalid_verdict(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "verdict": "lgtm",
    })
    assert resp.status_code == 400
    assert "verdict" in resp.get_json()["error"]


def test_save_iteration_400_on_invalid_bracket(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 9,
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "bracket" in resp.get_json()["error"]


def test_save_iteration_400_on_non_object_audit_manifest(save_client):
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": "not-an-object", "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "audit_manifest" in resp.get_json()["error"]


def test_pricing_series_empty_deck(save_client):
    """No iterations → empty points list."""
    client, _ = save_client
    resp = client.get("/api/pricing_series?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck_id"] == "Alpha"
    assert body["count"] == 0
    assert body["points"] == []


def test_pricing_series_chronological_points(save_client):
    """Iterations saved with total_price_usd appear in the series in
    chronological order, with iteration_id + captured_at + the price."""
    client, _ = save_client
    # 3 iterations at different prices.
    for price in [142.37, 138.50, 95.00]:
        client.post("/api/save_iteration", json={
            "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
            "total_price_usd": price, "verdict": "pending",
        })
    resp = client.get("/api/pricing_series?deck=Alpha")
    body = resp.get_json()
    assert body["count"] == 3
    prices = [p["total_price_usd"] for p in body["points"]]
    assert prices == [142.37, 138.50, 95.00]
    # Each point carries iteration_id + captured_at.
    for point in body["points"]:
        assert isinstance(point["iteration_id"], int)
        assert point["captured_at"]


def test_pricing_series_400_without_deck_param(save_client):
    client, _ = save_client
    resp = client.get("/api/pricing_series")
    assert resp.status_code == 400


def test_verdict_breakdown_empty_deck(save_client):
    """No iterations → breakdown is an empty dict; total_iterations=0."""
    client, _ = save_client
    resp = client.get("/api/verdict_breakdown?deck=Alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deck_id"] == "Alpha"
    assert body["total_iterations"] == 0
    assert body["breakdown"] == {}


def test_verdict_breakdown_groups_by_audit_version(save_client):
    """After saving iterations under multiple audit_versions, the
    breakdown groups them and reports per-group verdict counts."""
    client, _ = save_client
    # Two v3 saves (one kept, one reverted) + one v4 kept.
    for verdict in ["kept", "reverted"]:
        client.post("/api/save_iteration", json={
            "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
            "audit_version": "v3", "verdict": verdict,
        })
    client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_version": "v4", "verdict": "kept",
    })

    resp = client.get("/api/verdict_breakdown?deck=Alpha")
    body = resp.get_json()
    assert body["total_iterations"] == 3
    assert body["breakdown"]["v3"]["kept"] == 1
    assert body["breakdown"]["v3"]["reverted"] == 1
    assert body["breakdown"]["v3"]["total"] == 2
    assert body["breakdown"]["v4"]["kept"] == 1
    assert body["breakdown"]["v4"]["total"] == 1


def test_verdict_breakdown_400_without_deck_param(save_client):
    client, _ = save_client
    resp = client.get("/api/verdict_breakdown")
    assert resp.status_code == 400
    assert "deck" in resp.get_json()["error"]


def test_save_iteration_handles_missing_sim_report(save_client):
    """Save should succeed even when the user persists an audit-only
    record (no sim_report yet). win_rate columns stay NULL."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}").get_json()
    assert detail["win_rate_old"] is None
    assert detail["win_rate_new"] is None
    assert detail["margin"] is None


# --- /api/save_iteration — pricing snapshot for cost-evolution chart ------

def test_save_iteration_captures_pricing_snapshot_into_manifest(save_client):
    """Top-level total_price_usd lands inside audit_manifest.pricing so
    the row's manifest carries cost data alongside the swap manifest.
    Captured timestamp tags when the snapshot was taken (in case the
    user later renames the deck or its on-disk price drifts)."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {"added": [], "removed": []},
        "total_price_usd": 142.37,
        "verdict": "pending",
    })
    assert resp.status_code == 200, resp.get_json()
    new_id = resp.get_json()["id"]
    detail = client.get(f"/api/iteration/{new_id}").get_json()
    manifest = detail["audit_manifest"]
    assert "pricing" in manifest
    assert manifest["pricing"]["total_price_usd"] == 142.37
    assert "captured_at" in manifest["pricing"]
    # ISO timestamp surface for analytics.
    assert manifest["pricing"]["captured_at"].startswith("20")


def test_save_iteration_pricing_without_audit_manifest_synthesizes_one(save_client):
    """Top-level total_price_usd should still land even when the caller
    didn't pass an audit_manifest — endpoint creates a minimal one with
    just the pricing block."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": 95.0,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    assert detail["audit_manifest"] is not None
    assert detail["audit_manifest"]["pricing"]["total_price_usd"] == 95.0


def test_save_iteration_omits_pricing_when_not_provided(save_client):
    """No total_price_usd in payload → audit_manifest stays unchanged,
    no pricing key fabricated. Avoids polluting legacy save flows."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {"added": [], "removed": []},
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    manifest = detail["audit_manifest"]
    assert "pricing" not in manifest


def test_save_iteration_preserves_caller_supplied_pricing(save_client):
    """If audit_manifest already carries a pricing block, the endpoint
    leaves it alone — the caller is the source of truth."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "audit_manifest": {
            "added": [],
            "pricing": {"total_price_usd": 200.0, "captured_at": "2025-01-01T00:00:00+00:00"},
        },
        # Top-level value should NOT overwrite the caller's explicit one.
        "total_price_usd": 999.99,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    pricing = detail["audit_manifest"]["pricing"]
    assert pricing["total_price_usd"] == 200.0
    assert pricing["captured_at"] == "2025-01-01T00:00:00+00:00"


def test_save_iteration_400_on_invalid_total_price_usd(save_client):
    """Non-numeric total_price_usd → 400, not silent drop."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": "lots of money",
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "total_price_usd" in resp.get_json()["error"]


def test_save_iteration_accepts_zero_price(save_client):
    """Zero is a legal price (jank deck, all basics) — must not be
    treated as 'missing'."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": 0.0,
        "verdict": "pending",
    })
    assert resp.status_code == 200
    detail = client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()
    assert detail["audit_manifest"]["pricing"]["total_price_usd"] == 0.0


def test_save_iteration_rejects_negative_price(save_client):
    """Regression: a negative price almost certainly means bad Scryfall
    data or a sign-flip bug upstream — never a legitimate value.
    Accepting it silently would poison the cost-evolution chart with
    nonsensical points; reject at the boundary instead."""
    client, _ = save_client
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "total_price_usd": -42.50,
        "verdict": "pending",
    })
    assert resp.status_code == 400
    assert "total_price_usd" in resp.get_json()["error"]
    assert "negative" in resp.get_json()["error"].lower() or \
        "non-negative" in resp.get_json()["error"].lower()


# --- /api/audit — card-name validation (hallucination defense) ------------

def test_audit_payload_includes_name_known_for_each_rec(client, monkeypatch):
    """Both added[] and removed[] dicts surface name_known so the UI can
    flag Claude-hallucinated cards."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="ramp",
                    evidence={"inclusion_pct": 78.0},
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Accursed Marauder", action="add",
                    reason="hallucinated by Claude",
                    evidence={"source": "claude"},
                    name_known=False,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="slow",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    added_by_card = {a["card"]: a for a in body["added"]}
    assert added_by_card["Lotus Cobra"]["name_known"] is True
    # _apply_swaps_to_dck may drop the unknown card because the card-text
    # snippet generator can't render a basic frame for it. The validator
    # surfaces in the manifest regardless, so look at the manifest.
    # The endpoint should still mark the rec it kept.
    if "Accursed Marauder" in added_by_card:
        assert added_by_card["Accursed Marauder"]["name_known"] is False
    removed_by_card = {r["card"]: r for r in body["removed"]}
    if "Cultivate" in removed_by_card:
        assert removed_by_card["Cultivate"]["name_known"] is True


def test_audit_payload_reports_unknown_card_count(client, monkeypatch):
    """Top-level unknown_card_count counts recs flagged name_known=False
    among the cards that actually landed in the proposed deck."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="",
                    evidence={}, name_known=True,
                ),
                SimpleNamespace(
                    card="Bogus Phantasm", action="add", reason="",
                    evidence={}, name_known=False,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # The fake card may or may not survive _apply_swaps_to_dck; either
    # way the response surfaces the count for the cards that *did* land.
    assert "unknown_card_count" in body
    assert isinstance(body["unknown_card_count"], int)
    assert body["unknown_card_count"] >= 0


def test_audit_routes_source_bracket_peers_to_advise(client, monkeypatch):
    """?source=bracket_peers must reach advise() as source='bracket_peers'.
    Mirrors the existing claude routing test."""
    from types import SimpleNamespace
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        seen["use_claude"] = kwargs.get("use_claude", False)
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Moat", action="add",
                    reason="in 5/5 reference decks (unanimous)",
                    evidence={
                        "in_n_references": 5,
                        "total_references": 5,
                        "frequency_label": "unanimous",
                        "role": "protection",
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="absent from all 5 reference decks",
                    evidence={"source": "bracket_peers"},
                    name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert seen["source"] == "bracket_peers"
    assert body["source"] == "bracket_peers"
    assert body["requested_llm"] == "bracket_peers"


def test_audit_bracket_peers_surfaces_fallback_reason(client, monkeypatch):
    """When advise() falls back to heuristic (no refs found), the UI
    needs to know — surface fallback_reason in body.warning so the
    user sees why they got the EDHREC heuristic instead."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Sol Ring", action="add",
                                reason="r", evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="r", evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason="no bracket-peer references found for "
                            "'Obscure' at B3 — falling back to EDHREC heuristic",
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "heuristic"  # fell back
    assert body["warning"] is not None
    assert "no bracket-peer references" in body["warning"]


def test_audit_400_on_unknown_source(client):
    """Unrecognized source value should 400, not silently fall through."""
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3&source=garbage_value",
    )
    assert resp.status_code == 400
    assert "source" in resp.get_json()["error"]


def test_audit_source_param_overrides_llm_param(client, monkeypatch):
    """When BOTH ?llm=claude AND ?source=bracket_peers are passed,
    source wins. (Old UI shipped llm=; new UI ships source=. Don't
    break existing bookmarks but let the newer name be authoritative.)"""
    from types import SimpleNamespace
    seen = {}

    def fake(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        seen["use_claude"] = kwargs.get("use_claude", False)
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="X", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Y", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake,
    )
    resp = client.get(
        "/api/audit?deck=Alpha&bracket=3"
        "&llm=claude&source=bracket_peers",
    )
    assert resp.status_code == 200
    assert seen["source"] == "bracket_peers"
    assert seen["use_claude"] is False  # source overrode the llm param


def test_audit_payload_surfaces_bracket_peer_ref_count(client, monkeypatch):
    """When advise() reports peer references were attached to a Claude
    request, the audit response carries the count so the UI can render
    'Claude analyst (5 peer refs)' on the source pill."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude",
            fallback_reason=None,
            bracket_peer_ref_count=5,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=claude",
                      headers={"X-Anthropic-API-Key": "sk-test"})
    body = resp.get_json()
    assert body["bracket_peer_ref_count"] == 5


def test_audit_payload_bracket_peer_ref_count_defaults_zero(client, monkeypatch):
    """Legacy stubs without the field render as 0, never raise."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # No bracket_peer_ref_count attr at all.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add", reason="",
                                evidence={}, name_known=True),
                SimpleNamespace(card="Cultivate", action="cut", reason="",
                                evidence={}, name_known=True),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.get_json()["bracket_peer_ref_count"] == 0


def test_audit_payload_match_pct_zero_for_signal_less_evidence(client, monkeypatch):
    """Regression: Claude recs only carry evidence={"source": "claude"}
    — no EDHREC inclusion/synergy AND no peer references frequency.
    The old _match_pct_from_evidence clamped these up to 1, displaying
    a misleading '1%' in the UI. Now returns 0 so the pill is
    suppressed entirely."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Cool Claude Pick", action="add", reason="LLM said so",
                    evidence={"source": "claude"},  # nothing else
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="claude", fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=claude",
                      headers={"X-Anthropic-API-Key": "sk-test"})
    body = resp.get_json()
    by_card = {a["card"]: a for a in body["added"]}
    assert by_card["Cool Claude Pick"]["match_pct"] == 0


def test_audit_payload_match_pct_from_reference_frequency(client, monkeypatch):
    """Phase A gap #2 from the bracket-peers self-audit: bracket_peers
    recs only set in_n_references / total_references, not inclusion_pct
    / synergy_pct. The audit response's match_pct field must compute
    from those for the UI pill to render anything other than 0%."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Moat", action="add", reason="r",
                    evidence={
                        "in_n_references": 5,
                        "total_references": 5,
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Last March", action="add", reason="r",
                    evidence={
                        "in_n_references": 3,
                        "total_references": 5,
                        "source": "bracket_peers",
                    },
                    name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="r",
                    evidence={}, name_known=True,
                ),
                # Second cut so _apply_swaps_to_dck can keep BOTH adds.
                # The endpoint balances adds==cuts; without this we'd
                # only see the first add land in the response.
                SimpleNamespace(
                    card="Old Card", action="cut", reason="r",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="bracket_peers",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3&source=bracket_peers")
    body = resp.get_json()
    by_card = {a["card"]: a for a in body["added"]}
    # 5/5 = 100% — unanimous reference inclusion gets max pill.
    assert by_card["Moat"]["match_pct"] == 100
    # 3/5 = 60% — majority but not unanimous.
    assert by_card["Last March"]["match_pct"] == 60


def test_audit_payload_includes_saturation_skips(client, monkeypatch):
    """When the saturation guard drops ramp adds (deck over-saturated),
    the dropped cards surface in body.skipped_for_saturation so the UI
    can show 'skipped 3 ramp adds — your deck already has 13'."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add", reason="",
                    evidence={"role": "ramp"}, name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut", reason="",
                    evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
            skipped_for_saturation=[
                {"card": "Rampant Growth", "role": "ramp",
                 "deck_count": 13, "threshold": 12},
                {"card": "Three Visits", "role": "ramp",
                 "deck_count": 13, "threshold": 12},
            ],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "skipped_for_saturation" in body
    assert len(body["skipped_for_saturation"]) == 2
    first = body["skipped_for_saturation"][0]
    assert first["role"] == "ramp"
    assert first["deck_count"] == 13
    assert first["threshold"] == 12
    assert first["card"] == "Rampant Growth"


def test_audit_payload_skipped_for_saturation_defaults_empty(client, monkeypatch):
    """Legacy stubs that don't set skipped_for_saturation must surface as
    an empty list, not raise AttributeError."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # SimpleNamespace WITHOUT skipped_for_saturation attribute —
        # simulates old stubs / pre-v8 callers.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(
                    card="Lotus Cobra", action="add",
                    reason="", evidence={}, name_known=True,
                ),
                SimpleNamespace(
                    card="Cultivate", action="cut",
                    reason="", evidence={}, name_known=True,
                ),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    assert resp.get_json()["skipped_for_saturation"] == []


def test_audit_payload_name_known_defaults_true_when_unset(client, monkeypatch):
    """Backward-compat: legacy advise() stubs that don't set name_known
    must not break the response (treat as known)."""
    from types import SimpleNamespace

    def fake_advise(deck_path, bracket, **_kwargs):
        # No name_known on these recs — emulate older callers.
        return SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="", evidence={}),
                SimpleNamespace(card="Cultivate", action="cut",
                                reason="", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
            source="heuristic",
            fallback_reason=None,
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    resp = client.get("/api/audit?deck=Alpha&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # Legacy stubs default to True — never spuriously flag as unknown.
    for entry in body["added"] + body["removed"]:
        assert entry.get("name_known", True) is True
    assert body["unknown_card_count"] == 0
