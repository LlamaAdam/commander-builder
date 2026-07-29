"""Tests for EDHREC time-windowed top-cards (fetch_top_cards)."""
from __future__ import annotations

import json

import pytest

from commander_builder import edhrec_client
from commander_builder.edhrec_client import CardEntry, fetch_top_cards


_TOP_JSON = json.dumps({
    "header": "Top Cards",
    "container": {"json_dict": {"cardlists": [
        {"header": "Past 2 Years", "cardviews": [
            {"name": "Sol Ring", "inclusion": 7456507, "num_decks": 7456507},
            {"name": "Arcane Signet", "inclusion": 6000000, "num_decks": 6000000},
            {"name": "Command Tower", "inclusion": 5000000, "num_decks": 5000000},
        ]},
    ]}},
})


@pytest.fixture
def no_cache(tmp_path, monkeypatch):
    # Redirect the cache dir so tests never read/write the real one.
    monkeypatch.setattr(edhrec_client, "CACHE_DIR", tmp_path / "edhrec")
    monkeypatch.setattr(edhrec_client, "REQUEST_SLEEP_SEC", 0)


def test_fetch_top_cards_parses_and_ranks(no_cache, monkeypatch):
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry",
                        lambda url: _TOP_JSON)
    cards = fetch_top_cards("year")
    assert [c.name for c in cards] == ["Sol Ring", "Arcane Signet", "Command Tower"]
    assert cards[0].num_decks == 7456507
    assert all(isinstance(c, CardEntry) for c in cards)


def test_fetch_top_cards_ranks_by_num_decks(no_cache, monkeypatch):
    # Out-of-order input → sorted desc by num_decks.
    j = json.dumps({"cardlists": [{"header": "x", "cardviews": [
        {"name": "B", "num_decks": 10}, {"name": "A", "num_decks": 99},
        {"name": "C", "num_decks": 50},
    ]}]})
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", lambda url: j)
    assert [c.name for c in fetch_top_cards("month")] == ["A", "C", "B"]


def test_fetch_top_cards_dedups(no_cache, monkeypatch):
    j = json.dumps({"cardlists": [
        {"header": "s1", "cardviews": [{"name": "Sol Ring", "num_decks": 100}]},
        {"header": "s2", "cardviews": [{"name": "Sol Ring", "num_decks": 100}]},
    ]})
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", lambda url: j)
    cards = fetch_top_cards("week")
    assert len(cards) == 1


def test_fetch_top_cards_network_failure_returns_empty(no_cache, monkeypatch):
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", boom)
    assert fetch_top_cards("year") == []


def test_fetch_top_cards_uses_cache(no_cache, monkeypatch):
    calls = {"n": 0}
    def once(url):
        calls["n"] += 1
        return _TOP_JSON
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", once)
    fetch_top_cards("year")          # fetch + write cache
    fetch_top_cards("year")          # served from cache
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# fetch_top_commanders — same JSON-API machinery, commanders/<slug> page
# ---------------------------------------------------------------------------

_COMMANDERS_JSON = json.dumps({
    "container": {"json_dict": {"cardlists": [
        {"header": "Top Commanders", "cardviews": [
            {"name": "Ms. Bumbleflower", "num_decks": 20000},
            {"name": "Atraxa, Praetors' Voice", "num_decks": 50000},
            {"name": "Tergrid, God of Fright", "num_decks": 30000},
        ]},
    ]}},
})


def test_fetch_top_commanders_parses_and_ranks(no_cache, monkeypatch):
    seen = {}

    def fake(url):
        seen["url"] = url
        return _COMMANDERS_JSON

    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", fake)
    commanders = edhrec_client.fetch_top_commanders("year")
    assert [c.name for c in commanders] == [
        "Atraxa, Praetors' Voice",
        "Tergrid, God of Fright",
        "Ms. Bumbleflower",
    ]
    # Commander ranking lives on the commanders/<slug> JSON page, not
    # top/<slug> (which ranks CARDS).
    assert seen["url"].endswith("/pages/commanders/year.json")


def test_fetch_top_commanders_network_failure_returns_empty(
        no_cache, monkeypatch):
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", boom)
    assert edhrec_client.fetch_top_commanders("year") == []


def test_fetch_top_commanders_empty_parse_not_cached(no_cache, monkeypatch):
    """No-cache-on-empty convention: a 200-OK page with no recognizable
    cardlists must not poison the cache — the next call refetches."""
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"nothing": "here"})
        return _COMMANDERS_JSON

    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", flaky)
    assert edhrec_client.fetch_top_commanders("year") == []
    commanders = edhrec_client.fetch_top_commanders("year")
    assert calls["n"] == 2
    assert commanders and commanders[0].name == "Atraxa, Praetors' Voice"


def test_fetch_top_commanders_uses_cache(no_cache, monkeypatch):
    calls = {"n": 0}

    def once(url):
        calls["n"] += 1
        return _COMMANDERS_JSON

    monkeypatch.setattr(edhrec_client, "_http_get_text_with_retry", once)
    edhrec_client.fetch_top_commanders("year")   # fetch + write cache
    edhrec_client.fetch_top_commanders("year")   # served from cache
    assert calls["n"] == 1
