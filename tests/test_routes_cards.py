"""Tests for the FP-007 ``/api/card/<name>`` card-reference endpoint."""
from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app


@pytest.fixture
def deck_dir(tmp_path):
    d = tmp_path / "decks"
    d.mkdir()
    (d / "Stub.dck").write_text(
        "[metadata]\nName=stub\n[Commander]\n1 Test\n[Main]\n1 Forest\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def client(deck_dir):
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


_SOL_RING = {
    "name": "Sol Ring",
    "mana_cost": "{1}",
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "cmc": 1.0,
    "color_identity": [],
    "legalities": {"commander": "legal", "modern": "not_legal"},
    "prices": {"usd": "1.49"},
    "set": "cmm",
    "set_name": "Commander Masters",
    "collector_number": "447",
    "rarity": "uncommon",
}


def test_card_route_projects_reference_fields(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: dict(_SOL_RING),
    )
    body = client.get("/api/card/Sol Ring").get_json()
    assert body["name"] == "Sol Ring"
    assert body["type_line"] == "Artifact"
    assert body["oracle_text"].startswith("{T}")
    assert body["color_identity"] == []          # colorless -> empty list, not None
    assert body["commander_legal"] is True        # projected from legalities
    assert body["price_usd"] == 1.49              # string -> float
    assert body["set"] == "cmm" and body["collector_number"] == "447"
    assert body["rarity"] == "uncommon"


def test_card_route_commander_illegal_and_no_price(client, monkeypatch):
    card = dict(_SOL_RING)
    card["legalities"] = {"commander": "banned"}
    card["prices"] = {"usd": None}
    card["color_identity"] = ["W", "U"]
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: card,
    )
    body = client.get("/api/card/Whatever").get_json()
    assert body["commander_legal"] is False
    assert body["price_usd"] is None
    assert body["color_identity"] == ["W", "U"]


def test_card_route_404_on_unknown_card(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    resp = client.get("/api/card/Notacard")
    assert resp.status_code == 404


def test_card_route_502_on_lookup_error(client, monkeypatch):
    def _boom(name, **kw):
        raise OSError("scryfall down")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    resp = client.get("/api/card/Sol Ring")
    assert resp.status_code == 502


def test_card_route_handles_split_name(client, monkeypatch):
    seen = {}

    def _fake(name, **kw):
        seen["name"] = name
        return dict(_SOL_RING, name=name)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    # ``//`` in split-card names must survive the path converter.
    resp = client.get("/api/card/Fire // Ice")
    assert resp.status_code == 200
    assert seen["name"] == "Fire // Ice"
