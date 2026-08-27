"""Where the meta blueprint writes its side files (P21).

The browser-error sink (``POST /api/log_error``) used to resolve to
``deck_dir.parent.parent / "_js_errors.log"`` — two levels ABOVE the
deck directory. That is not a location the app owns:

  * bundled install → ``vendor/`` (wiped on upgrade),
  * default deck dir (``~/Documents/CommanderBuilder/decks``) →
    ``~/Documents``,
  * a deck dir the user picked (``/mnt/share/mtg/decks``) → an
    app-private log dropped in ``/mnt/`` territory belonging to
    someone else.

It now lives in the user config home — ``~/.commander-builder`` (or
``%LOCALAPPDATA%\\commander-builder``), resolved through
``config_store.config_path()`` so it follows credentials / config.json
/ replays / collection.txt and honors the ``COMMANDER_BUILDER_CONFIG``
override.

Offline: no network, no real home directory — every test points
``COMMANDER_BUILDER_CONFIG`` at a tmp path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app
from commander_builder.web.routes_meta import _js_error_log_path


def _write_deck(deck_dir: Path, name: str) -> Path:
    p = deck_dir / f"{name}.dck"
    p.write_text(
        "[metadata]\n"
        f"Name={name}\n\n"
        "[Commander]\n1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 35,
        encoding="utf-8",
    )
    return p


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    """Point the per-user config store at a tmp dir. Its PARENT is the
    config home, which is where the error sink now writes."""
    home = tmp_path / "cfghome"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(home / "config.json"))
    return home


@pytest.fixture
def deck_dir(tmp_path) -> Path:
    """A deck dir nested deep enough that ``parent.parent`` is a
    distinct, obviously-not-ours directory (the old sink location)."""
    d = tmp_path / "somewhere" / "else" / "decks"
    d.mkdir(parents=True)
    _write_deck(d, "Alpha")
    return d


@pytest.fixture
def client(deck_dir, config_home):
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# --- path resolution -------------------------------------------------------

def test_js_error_log_lives_in_the_user_config_home(config_home):
    assert _js_error_log_path() == config_home / "_js_errors.log"


def test_js_error_log_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """No import-time freezing (the DEFAULT_DB_PATH lesson): an env
    override applied after import must still steer the write."""
    monkeypatch.setenv(
        "COMMANDER_BUILDER_CONFIG", str(tmp_path / "a" / "config.json"),
    )
    assert _js_error_log_path().parent == tmp_path / "a"
    monkeypatch.setenv(
        "COMMANDER_BUILDER_CONFIG", str(tmp_path / "b" / "config.json"),
    )
    assert _js_error_log_path().parent == tmp_path / "b"


# --- the route ------------------------------------------------------------

def test_log_error_writes_to_the_config_home_not_above_the_deck_dir(
    client, config_home, deck_dir,
):
    resp = client.post("/api/log_error", json={
        "message": "boom", "url": "http://x/y", "kind": "error",
        "stack": "at f()", "user_agent": "pytest",
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    log_path = config_home / "_js_errors.log"
    body = log_path.read_text(encoding="utf-8")
    assert "MSG: boom" in body
    assert "STACK:" in body
    # The old location is untouched — including the parent directories
    # of a deck dir the user chose.
    assert not (deck_dir.parent.parent / "_js_errors.log").exists()


def test_log_error_creates_the_config_home_if_absent(client, config_home):
    """First run on a fresh machine has no ``~/.commander-builder`` yet;
    the sink must create it rather than 500."""
    assert not config_home.exists()
    resp = client.post("/api/log_error", json={"message": "first ever"})
    assert resp.status_code == 200
    assert (config_home / "_js_errors.log").exists()


def test_log_error_leaves_a_breadcrumb_pointing_at_the_old_log(
    client, config_home, deck_dir,
):
    """Graceful migration: an operator who finds the new log must be
    able to find the OLD entries. One note, on the first write only —
    and the old file is left exactly where it is (moving it is the same
    overreach the fix removes)."""
    legacy = deck_dir.parent.parent / "_js_errors.log"
    legacy.write_text("--- 2026-01-01T00:00:00 [error] old entry\n", "utf-8")
    legacy_before = legacy.read_text(encoding="utf-8")

    assert client.post("/api/log_error", json={"message": "new one"}).status_code == 200
    body = (config_home / "_js_errors.log").read_text(encoding="utf-8")
    assert "[migration]" in body
    assert str(legacy) in body
    assert body.count("[migration]") == 1
    # Old file neither moved nor rewritten.
    assert legacy.exists()
    assert legacy.read_text(encoding="utf-8") == legacy_before

    # Second write: the new log exists now, so no repeat note.
    assert client.post("/api/log_error", json={"message": "second"}).status_code == 200
    body2 = (config_home / "_js_errors.log").read_text(encoding="utf-8")
    assert body2.count("[migration]") == 1
    assert "MSG: second" in body2


def test_log_error_writes_no_breadcrumb_without_a_legacy_log(
    client, config_home,
):
    client.post("/api/log_error", json={"message": "clean install"})
    body = (config_home / "_js_errors.log").read_text(encoding="utf-8")
    assert "[migration]" not in body


def test_log_error_byte_cap_still_applies_at_the_new_location(
    client, config_home, monkeypatch,
):
    """The cap is the reason this endpoint can't be a disk-filler; it
    must follow the file to its new home."""
    from commander_builder.web import routes_meta
    monkeypatch.setattr(routes_meta, "_JS_ERROR_LOG_MAX_BYTES", 200)

    log_path = config_home / "_js_errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("x" * 500, encoding="utf-8")

    resp = client.post("/api/log_error", json={"message": "over the cap"})
    assert resp.status_code == 200
    assert resp.get_json() == {"logged": False, "reason": "log full"}
    assert log_path.read_text(encoding="utf-8") == "x" * 500


def test_log_error_still_rejects_a_body_without_a_message(client):
    assert client.post("/api/log_error", json={"kind": "error"}).status_code == 400
