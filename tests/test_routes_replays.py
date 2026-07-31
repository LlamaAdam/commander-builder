"""FP-016 replay-lite — web route + nav markup tests.

Covers:

- GET /api/replays: shape, newest-first ordering, index-less dirs
  skipped, enabled flag surfaced, JSON content type.
- GET /api/replay/<run>/<game>: full timeline payload, clean JSON 404s
  (unknown run, unknown game, unreadable ids), path-traversal safety
  (separator smuggling, dot-dot components, absolute paths).
- Left-rail nav markup: the Replays rail button + sidebar section +
  viewer pane follow the PR #36 a11y conventions (aria-pressed rail
  state, aria-live results region, labelled viewer landmark) and the
  replays.js asset is wired + served.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from commander_builder import replay_store
from commander_builder.replay_store import (
    ENV_KEEP_LOGS,
    ENV_REPLAY_DIR,
    INDEX_NAME,
    ReplayRun,
)
from commander_builder.web.app import create_app
from commander_builder.web.routes_replays import _safe_run_dir

FIXTURES = Path(__file__).parent / "fixtures" / "replays"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def replay_root_dir(tmp_path, monkeypatch):
    root = tmp_path / "replays"
    monkeypatch.setenv(ENV_REPLAY_DIR, str(root))
    monkeypatch.delenv(ENV_KEEP_LOGS, raising=False)
    replay_store._reset_process_run_for_tests()
    yield root
    replay_store._reset_process_run_for_tests()


@pytest.fixture
def seeded_root(replay_root_dir):
    """Two runs recorded through the real store API: an older single-game
    run and a newer multi-game run (complete + draw + truncated)."""
    old_run = ReplayRun(replay_root_dir, run_id="20250101T000000Z_1_aaaaaa")
    old_run.record_stdout(
        (FIXTURES / "complete_game.log").read_text(encoding="utf-8"),
        deck_filenames=["A.dck", "B.dck", "C.dck", "D.dck"],
        source="seed_old",
    )
    new_run = ReplayRun(replay_root_dir, run_id="20260101T000000Z_1_bbbbbb")
    new_run.record_stdout(
        (FIXTURES / "multi_game.log").read_text(encoding="utf-8"),
        deck_filenames=["A.dck", "B.dck", "C.dck", "D.dck"],
        source="seed_new",
    )
    return replay_root_dir


@pytest.fixture
def client(tmp_path, seeded_root):
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /api/replays
# ---------------------------------------------------------------------------

def test_list_replays_shape_and_order(client):
    resp = client.get("/api/replays")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    body = resp.get_json()
    assert body["count"] == 2
    assert body["enabled"] is False  # capture flag is off in this env
    runs = body["runs"]
    # Newest first (ids are timestamp-prefixed).
    assert [r["run"] for r in runs] == [
        "20260101T000000Z_1_bbbbbb", "20250101T000000Z_1_aaaaaa",
    ]
    newest = runs[0]
    assert newest["count"] == 3
    assert newest["cap_reached"] is False
    games = newest["games"]
    assert [g["game"] for g in games] == [1, 2, 3]
    assert games[0]["winner_name"] == "Alpha Ramp [B3]"
    assert games[1]["is_draw"] is True
    assert games[2]["truncated"] is True
    # Summary rows stay lean — no eliminations blob in the list view.
    assert "eliminations" not in games[0]


def test_list_replays_empty_store(replay_root_dir, tmp_path):
    deck_dir = tmp_path / "decks2"
    deck_dir.mkdir()
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    body = app.test_client().get("/api/replays").get_json()
    assert body == {"runs": [], "count": 0, "enabled": False}


def test_list_replays_skips_indexless_dirs(client, seeded_root):
    (seeded_root / "20270101T000000Z_9_zzzzzz").mkdir()
    body = client.get("/api/replays").get_json()
    assert body["count"] == 2  # bare dir isn't a browsable run


# ---------------------------------------------------------------------------
# GET /api/replay/<run>/<game>
# ---------------------------------------------------------------------------

def test_get_replay_returns_timeline(client):
    resp = client.get("/api/replay/20250101T000000Z_1_aaaaaa/1")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    body = resp.get_json()
    assert body["run"] == "20250101T000000Z_1_aaaaaa"
    assert body["game"] == 1
    assert body["meta"]["file"] == "game_1.log"
    assert body["meta"]["source"] == "seed_old"
    tl = body["timeline"]
    assert tl["truncated"] is False
    assert tl["result"]["winner_name"] == "Alpha Ramp [B3]"
    assert [t["turn"] for t in tl["turns"]] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_get_replay_truncated_game_flagged(client):
    body = client.get(
        "/api/replay/20260101T000000Z_1_bbbbbb/3").get_json()
    assert body["timeline"]["truncated"] is True
    assert body["meta"]["truncated"] is True


def test_get_replay_404_unknown_run(client):
    resp = client.get("/api/replay/20990101T000000Z_1_ffffff/1")
    assert resp.status_code == 404
    assert resp.content_type.startswith("application/json")
    assert "error" in resp.get_json()


def test_get_replay_404_unknown_game(client):
    resp = client.get("/api/replay/20250101T000000Z_1_aaaaaa/99")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_get_replay_404_non_numeric_game(client):
    resp = client.get("/api/replay/20250101T000000Z_1_aaaaaa/nope")
    assert resp.status_code == 404


def test_get_replay_game_zero_and_huge_rejected(client):
    assert client.get(
        "/api/replay/20250101T000000Z_1_aaaaaa/0").status_code == 404
    assert client.get(
        "/api/replay/20250101T000000Z_1_aaaaaa/9999999").status_code == 404


# ---------------------------------------------------------------------------
# Path-traversal safety
# ---------------------------------------------------------------------------

def test_safe_run_dir_rejects_traversal_ids(seeded_root):
    for bad in (
        "..", "...", "....", "a..b", "../secrets", "..\\secrets",
        "a/b", "a\\b", "C:", "C:\\Windows", "/etc", ".", "",
        "run\x00id", "run id",
    ):
        assert _safe_run_dir(bad) is None, bad


def test_safe_run_dir_accepts_real_run(seeded_root):
    d = _safe_run_dir("20250101T000000Z_1_aaaaaa")
    assert d is not None
    assert d.name == "20250101T000000Z_1_aaaaaa"


def test_traversal_via_url_cannot_escape_store(client, tmp_path):
    # A secret OUTSIDE the replay root that a traversal would reach.
    secret = tmp_path / "secret.log"
    secret.write_text("password", encoding="utf-8")
    for url in (
        "/api/replay/..%2F..%2Fsecret/1",
        "/api/replay/..%5C..%5Csecret/1",
        "/api/replay/%2e%2e/1",
    ):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert b"password" not in resp.data


# ---------------------------------------------------------------------------
# Nav shell markup + assets (PR #36 a11y conventions)
# ---------------------------------------------------------------------------

def test_root_has_replays_rail_button_with_aria(client):
    body = client.get("/").data.decode("utf-8")
    i = body.index('data-section="replays"')
    tag = body[body.rindex("<button", 0, i):body.index(">", i) + 1]
    assert 'class="rail-btn"' in tag
    assert 'aria-label="Replays"' in tag
    assert 'aria-pressed="false"' in tag


def test_root_has_replays_sidebar_section(client):
    body = client.get("/").data.decode("utf-8")
    assert 'id="section-replays"' in body
    assert 'id="replays-refresh-btn"' in body
    # Results region announces load/refresh to screen readers.
    i = body.index('id="replays-run-list"')
    tag = body[body.rindex("<div", 0, i):body.index(">", i) + 1]
    assert 'aria-live="polite"' in tag


def test_root_has_labelled_replay_viewer_pane(client):
    body = client.get("/").data.decode("utf-8")
    i = body.index('id="replays-main"')
    tag = body[body.rindex("<section", 0, i):body.index(">", i) + 1]
    assert 'aria-label="Replay viewer"' in tag
    assert "hidden" in tag  # only visible in the Replays section


def test_replays_js_wired_and_served(client):
    body = client.get("/").data.decode("utf-8")
    assert "replays.js" in body
    resp = client.get("/static/replays.js")
    assert resp.status_code == 200
    js = resp.data.decode("utf-8")
    # XSS discipline: no innerHTML for server data beyond the static
    # loading/error strings; timeline content goes through el().
    assert "/api/replays" in js
    assert "/api/replay/" in js


def test_nav_js_knows_replays_section(client):
    js = client.get("/static/nav.js").data.decode("utf-8")
    assert '"replays"' in js
    assert "replays-main" in js
