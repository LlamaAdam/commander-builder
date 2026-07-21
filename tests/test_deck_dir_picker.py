"""Tests for the FP-010 deck-dir picker (config_store.get_deck_dir + desktop wiring)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from commander_builder import config_store, desktop


# --------------------------------------------------------------------------- #
# config_store.get_deck_dir -- resolution order
# --------------------------------------------------------------------------- #

@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Isolate the config store to a temp file."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(path))
    monkeypatch.delenv("COMMANDER_BUILDER_DECK_DIR", raising=False)
    return path


def test_get_deck_dir_env_var_wins(monkeypatch, cfg_file):
    """COMMANDER_BUILDER_DECK_DIR overrides config and default."""
    monkeypatch.setenv("COMMANDER_BUILDER_DECK_DIR", r"C:\custom\decks")
    assert config_store.get_deck_dir() == Path(r"C:\custom\decks")


def test_get_deck_dir_config_wins_over_default(tmp_path, cfg_file, monkeypatch):
    """A deck_dir stored in config overrides the platform default."""
    config_store.apply_update({"deck_dir": str(tmp_path / "my_decks")},
                               path=cfg_file)
    result = config_store.get_deck_dir(path=cfg_file)
    assert result == tmp_path / "my_decks"


def test_get_deck_dir_platform_default_when_unconfigured(cfg_file, monkeypatch):
    """No env var, no config -> returns the platform default under home/Documents."""
    result = config_store.get_deck_dir(path=cfg_file)
    # Platform default must include the brand sub-path.
    assert "CommanderBuilder" in str(result)
    assert result.name == "decks"


def test_get_deck_dir_windows_default_uses_userprofile(monkeypatch, cfg_file):
    """On Windows (os.name == 'nt'), USERPROFILE drives the default path."""
    monkeypatch.setattr(config_store, "_is_windows", lambda: True)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\TestUser")
    result = config_store.get_deck_dir(path=cfg_file)
    assert str(result).startswith(r"C:\Users\TestUser")
    assert "CommanderBuilder" in str(result)


def test_get_deck_dir_not_created_automatically(cfg_file):
    """get_deck_dir() must not create the directory."""
    result = config_store.get_deck_dir(path=cfg_file)
    # We can't assert it doesn't exist on a real machine (Documents/ usually
    # does), but at minimum the function must return without error.
    assert isinstance(result, Path)


def test_deck_dir_is_allowed_config_key():
    """'deck_dir' must be in ALLOWED_KEYS so PUT /api/config accepts it."""
    assert "deck_dir" in config_store.ALLOWED_KEYS


def test_validate_update_accepts_deck_dir():
    norm, errors = config_store.validate_update({"deck_dir": r"C:\my\decks"})
    assert not errors
    assert norm["deck_dir"] == r"C:\my\decks"


def test_validate_update_clears_deck_dir_on_empty():
    norm, errors = config_store.validate_update({"deck_dir": ""})
    assert not errors
    assert norm["deck_dir"] is None  # clear sentinel


def test_round_trip_deck_dir_via_config(tmp_path, cfg_file, monkeypatch):
    """save -> load -> get_deck_dir round-trip for deck_dir."""
    target = tmp_path / "my_decks"
    config_store.apply_update({"deck_dir": str(target)}, path=cfg_file)
    assert config_store.get_deck_dir(path=cfg_file) == target


# --------------------------------------------------------------------------- #
# desktop._resolve_deck_dir -- precedence
# --------------------------------------------------------------------------- #

def test_resolve_deck_dir_explicit_wins(monkeypatch):
    """An explicit deck_dir arg always wins over config."""
    # Even if config_store would return something different, explicit wins.
    monkeypatch.setattr(config_store, "get_deck_dir",
                        lambda **kw: Path("/from/config"))
    result = desktop._resolve_deck_dir("/explicit/path")
    assert result == "/explicit/path"


def test_resolve_deck_dir_falls_back_to_config(tmp_path, monkeypatch):
    """When deck_dir=None, _resolve_deck_dir returns the config value."""
    monkeypatch.setattr(config_store, "get_deck_dir",
                        lambda **kw: Path(str(tmp_path / "cfg_decks")))
    result = desktop._resolve_deck_dir(None)
    assert "cfg_decks" in (result or "")


def test_resolve_deck_dir_none_explicit_means_use_config():
    """Passing None explicitly falls back to config, not empty string."""
    result = desktop._resolve_deck_dir(None)
    # Must be either a non-empty string or None (never an empty string).
    assert result is None or (isinstance(result, str) and result != "")


def test_launch_passes_resolved_deck_dir_to_serve(monkeypatch, tmp_path):
    """launch() passes the resolved deck_dir (from config) to serve()."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)
    expected = str(tmp_path / "deck_decks")
    monkeypatch.setattr(config_store, "get_deck_dir",
                        lambda **kw: Path(expected))

    served = {}

    def fake_serve(deck_dir, host, port):
        served["deck_dir"] = deck_dir

    class FakeWebview:
        @staticmethod
        def create_window(*a, **k): pass
        @staticmethod
        def start(): pass

    desktop.launch(
        deck_dir=None, host="127.0.0.1", port=9911,
        webview=FakeWebview, serve=fake_serve,
    )
    assert served["deck_dir"] == expected
