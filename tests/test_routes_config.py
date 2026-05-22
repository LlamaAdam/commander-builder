"""Tests for the GET/PUT /api/config endpoints (FP-011)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("flask")

from commander_builder import config_store
from commander_builder.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("COMMANDER_BUILDER_CONFIG", str(cfg))
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    app = create_app(deck_dir=deck_dir, knowledge_db=tmp_path / "kl.sqlite")
    app.config["TESTING"] = True
    return app.test_client()


# --- GET ------------------------------------------------------------------

def test_get_empty_config(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["anthropic_api_key_set"] is False
    assert "anthropic_api_key" not in data


def test_get_redacts_token(client):
    config_store.save_config({"anthropic_api_key": "sk-ant-abcd1234efgh5678"})
    data = client.get("/api/config").get_json()
    assert data["anthropic_api_key_set"] is True
    assert data["anthropic_api_key_hint"] == "…5678"
    assert "anthropic_api_key" not in data  # raw token never leaves


# --- PUT ------------------------------------------------------------------

def test_put_sets_token_and_persists(client):
    resp = client.put("/api/config", json={
        "anthropic_api_key": "sk-ant-abcd1234efgh5678ijkl",
        "default_bracket": 4,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert set(body["updated"]) == {"anthropic_api_key", "default_bracket"}
    # Response is redacted...
    assert "anthropic_api_key" not in body["config"]
    assert body["config"]["anthropic_api_key_set"] is True
    # ...but the real token landed on disk.
    assert config_store.load_config()["anthropic_api_key"].startswith("sk-ant-")
    assert config_store.load_config()["default_bracket"] == 4


def test_put_rejects_malformed_token_without_persisting(client):
    resp = client.put("/api/config", json={"anthropic_api_key": "nope"})
    assert resp.status_code == 400
    assert "validation failed" in resp.get_json()["error"]
    assert config_store.load_config() == {}  # nothing written


def test_put_rejects_unknown_key(client):
    resp = client.put("/api/config", json={"evil": "x"})
    assert resp.status_code == 400
    assert any("unknown config key" in d for d in resp.get_json()["details"])


def test_put_rejects_non_object_body(client):
    resp = client.put("/api/config", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_put_clears_key_with_empty_string(client):
    config_store.save_config({"moxfield_user": "Bob", "model": "claude"})
    resp = client.put("/api/config", json={"moxfield_user": ""})
    assert resp.status_code == 200
    stored = config_store.load_config()
    assert "moxfield_user" not in stored
    assert stored["model"] == "claude"  # untouched


def test_put_partial_failure_persists_nothing(client):
    config_store.save_config({"model": "claude"})
    # one good key, one bad key -> whole request rejected
    resp = client.put("/api/config", json={
        "default_bracket": 2, "anthropic_api_key": "bad",
    })
    assert resp.status_code == 400
    assert config_store.load_config() == {"model": "claude"}  # unchanged
