"""Writer-side pins for audit open bug 2 (2026-09-09): every oracle
snapshot writer in THIS repo persists the Scryfall ``prices`` block.

The shared snapshot dir carries a mixed schema because ``forge_py``'s
trimmed writer (outside this repo) drops ``prices``. The reader
(``price_status`` / ``web.deck_pricing`` / the dashboard tile) now
counts and names such cards instead of dropping them — but the fix is
only whole if this repo never becomes a SECOND source of price-less
snapshots. These tests pin that ``lookup_card``'s network path,
``refresh_card`` and ``write_snapshots_from_bulk`` all write the block
verbatim, and that what they write is exactly what the reader prices.

Offline: the HTTP boundary is ``scryfall_client._http_get_json`` (the
same seam the client tests patch); the bulk writer reads a temp file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder import oracle_store, scryfall_client
from commander_builder.price_status import price_status

_PRICES = {"usd": "12.34", "usd_foil": "40.00", "usd_etched": None,
           "eur": "10.00", "tix": "0.02"}


def _full_card(name: str) -> dict:
    return {
        "object": "card", "name": name, "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.", "cmc": 1.0,
        "color_identity": [], "prices": dict(_PRICES),
        "legalities": {"commander": "legal"},
    }


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch) -> Path:
    snap = tmp_path / "oracle_snapshots"
    monkeypatch.setattr(scryfall_client, "CACHE_DIR", snap)
    monkeypatch.setattr(scryfall_client, "REQUEST_SLEEP_SEC", 0.0)
    return snap


def _on_disk(name: str) -> dict:
    return json.loads(
        scryfall_client._cache_path(name).read_text(encoding="utf-8"))


def test_lookup_card_network_path_writes_prices_verbatim(snapshot_dir, monkeypatch):
    monkeypatch.setattr(
        scryfall_client, "_http_get_json", lambda url: _full_card("Sol Ring"),
    )
    card = scryfall_client.lookup_card("Sol Ring")
    assert card["prices"] == _PRICES
    written = _on_disk("Sol Ring")
    assert written["prices"] == _PRICES
    assert price_status(written) == (12.34, None)


def test_refresh_card_writes_prices_verbatim(snapshot_dir, monkeypatch):
    # A stale trimmed snapshot on disk (the other writer's shape) is
    # REPLACED whole by the refresh, prices included.
    snapshot_dir.mkdir(parents=True)
    scryfall_client._cache_path("Sol Ring").write_text(
        json.dumps({"name": "Sol Ring", "type_line": "Artifact"}),
        encoding="utf-8",
    )
    assert price_status(_on_disk("Sol Ring"))[0] is None
    monkeypatch.setattr(
        scryfall_client, "_http_get_json", lambda url: _full_card("Sol Ring"),
    )
    scryfall_client.refresh_card("Sol Ring")
    written = _on_disk("Sol Ring")
    assert written["prices"] == _PRICES
    assert price_status(written) == (12.34, None)


def test_write_snapshots_from_bulk_keeps_prices(snapshot_dir, tmp_path):
    bulk = tmp_path / "oracle-cards.json"
    bulk.write_text(
        json.dumps([_full_card("Sol Ring"), _full_card("Arcane Signet")]),
        encoding="utf-8",
    )
    out = oracle_store.write_snapshots_from_bulk(
        ["Sol Ring", "Arcane Signet"], bulk_path=bulk,
    )
    assert out["written"] == 2 and out["missing"] == []
    for name in ("Sol Ring", "Arcane Signet"):
        written = _on_disk(name)
        assert written["prices"] == _PRICES, name
        assert price_status(written) == (12.34, None)


def test_no_repo_writer_projects_the_card_before_writing():
    """Belt and braces: the three write sites serialize the object they
    fetched/loaded, not a projection of it. If someone introduces a
    ``_trim_snapshot`` on the oracle path (the prints cache legitimately
    has one — ``_trim_printing`` — and it keeps ``prices``), this test
    asks them to keep ``prices`` and update the reader's WHY note."""
    import inspect
    src = inspect.getsource(scryfall_client.lookup_card)
    assert "json.dumps(data)" in src
    src = inspect.getsource(scryfall_client.refresh_card)
    assert "json.dumps(data)" in src
    src = inspect.getsource(oracle_store.write_snapshots_from_bulk)
    assert "json.dumps(card)" in src
    # The prints-cache projection is the one trim in the repo, and it
    # keeps the price keys the pricing layer reads.
    assert scryfall_client._trim_printing(_full_card("X"))["prices"]["usd"] == "12.34"
