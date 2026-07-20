"""Tests for the per-user config store (FP-011)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from commander_builder import config_store


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Point the config store at a temp file via the override env var."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(path))
    return path


# --- path resolution ------------------------------------------------------

def test_override_env_wins(cfg_file):
    assert config_store.config_path() == cfg_file


def test_localappdata_used_on_windows(monkeypatch):
    monkeypatch.delenv("COMMANDER_BUILDER_CONFIG", raising=False)
    monkeypatch.setattr(config_store, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    p = config_store.config_path()
    assert p.name == "config.json"
    assert "commander-builder" in str(p)


# --- load / save round-trip ----------------------------------------------

def test_load_missing_returns_empty(cfg_file):
    assert config_store.load_config() == {}

def test_load_corrupt_returns_empty(cfg_file):
    cfg_file.write_text("{not json", encoding="utf-8")
    assert config_store.load_config() == {}


def test_load_drops_unknown_keys(cfg_file):
    cfg_file.write_text(json.dumps({"model": "x", "junk": 1}), encoding="utf-8")
    assert config_store.load_config() == {"model": "x"}


def test_save_round_trip_and_perms(cfg_file):
    config_store.save_config({"model": "claude", "junk": "dropped"})
    assert config_store.load_config() == {"model": "claude"}
    if os.name != "nt":
        assert (cfg_file.stat().st_mode & 0o777) == 0o600


# --- redaction ------------------------------------------------------------

def test_redact_never_emits_raw_token(cfg_file):
    red = config_store.redact_config({"anthropic_api_key": "sk-ant-abcd1234efgh5678"})
    assert "anthropic_api_key" not in red
    assert red["anthropic_api_key_set"] is True
    assert red["anthropic_api_key_hint"] == "…5678"


def test_redact_unset_token(cfg_file):
    red = config_store.redact_config({})
    assert red["anthropic_api_key_set"] is False
    assert "anthropic_api_key_hint" not in red


def test_redact_passes_through_nonsecret(cfg_file):
    red = config_store.redact_config({"model": "claude", "default_bracket": 3})
    assert red["model"] == "claude"
    assert red["default_bracket"] == 3


# --- validation -----------------------------------------------------------

def test_validate_rejects_unknown_key():
    _, errors = config_store.validate_update({"nope": 1})
    assert errors and "unknown config key" in errors[0]


def test_validate_accepts_well_formed_token():
    norm, errors = config_store.validate_update(
        {"anthropic_api_key": "sk-ant-abcd1234efgh5678ijkl"})
    assert not errors
    assert norm["anthropic_api_key"].startswith("sk-ant-")


def test_validate_rejects_malformed_token():
    norm, errors = config_store.validate_update({"anthropic_api_key": "hunter2"})
    assert errors and "Anthropic API key" in errors[0]
    assert "anthropic_api_key" not in norm


def test_validate_empty_token_clears():
    norm, errors = config_store.validate_update({"anthropic_api_key": ""})
    assert not errors
    assert norm["anthropic_api_key"] is None  # clear sentinel


def test_validate_bracket_range():
    _, errors = config_store.validate_update({"default_bracket": 9})
    assert errors and "1-5" in errors[0]
    norm, errors = config_store.validate_update({"default_bracket": "4"})
    assert not errors and norm["default_bracket"] == 4


def test_validate_string_key():
    norm, errors = config_store.validate_update({"moxfield_user": "Alice"})
    assert not errors and norm["moxfield_user"] == "Alice"


# --- apply_update merge semantics -----------------------------------------

def test_apply_update_sets_and_clears(cfg_file):
    config_store.apply_update({"model": "claude", "moxfield_user": "Bob"})
    assert config_store.load_config() == {"model": "claude", "moxfield_user": "Bob"}
    # None clears just that key, leaves the rest.
    config_store.apply_update({"moxfield_user": None})
    assert config_store.load_config() == {"model": "claude"}


def test_apply_update_token_persists_but_redacts(cfg_file):
    config_store.apply_update({"anthropic_api_key": "sk-ant-abcd1234efgh5678"})
    stored = config_store.load_config()
    assert stored["anthropic_api_key"] == "sk-ant-abcd1234efgh5678"
    red = config_store.redact_config(stored)
    assert "anthropic_api_key" not in red and red["anthropic_api_key_set"]
