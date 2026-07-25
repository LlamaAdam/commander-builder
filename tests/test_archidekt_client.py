"""Tests for archidekt_client — fully offline via injected fetch_json."""

from __future__ import annotations

import pytest

from commander_builder.archidekt_client import (
    DEFAULT_N,
    extract_mainboard,
    fetch_top_decks,
)


def card(name, quantity=1, categories=()):
    return {"quantity": quantity, "categories": list(categories),
            "card": {"oracleCard": {"name": name}}}


def detail(cards, categories=()):
    return {"cards": cards, "categories": list(categories)}


def search_hit(deck_id, size=100, edh_bracket=None):
    return {"id": deck_id, "size": size, "edhBracket": edh_bracket}


# ---------------------------------------------------------------------------
# extract_mainboard
# ---------------------------------------------------------------------------

def test_mainboard_extracts_names():
    d = detail([card("Sol Ring"), card("Skirk Prospector")])
    assert extract_mainboard(d) == ["Sol Ring", "Skirk Prospector"]


def test_mainboard_skips_commander_category():
    d = detail([card("Krenko, Mob Boss", categories=["Commander"]),
                card("Sol Ring", categories=["Artifact"])],
               categories=[{"name": "Commander", "includedInDeck": True}])
    assert extract_mainboard(d) == ["Sol Ring"]


def test_mainboard_honors_included_in_deck_false():
    d = detail(
        [card("Sol Ring"), card("Wishlist Card", categories=["Maybeboard"])],
        categories=[{"name": "Maybeboard", "includedInDeck": False}],
    )
    assert extract_mainboard(d) == ["Sol Ring"]


def test_mainboard_category_matching_is_case_insensitive():
    d = detail(
        [card("Wish", categories=["MAYBEBOARD"])],
        categories=[{"name": "maybeboard", "includedInDeck": False}],
    )
    assert extract_mainboard(d) == []


def test_mainboard_skips_zero_quantity_and_malformed():
    d = detail([card("Ghost", quantity=0), "not-a-dict",
                {"quantity": 1, "card": {}}, card("Real")])
    assert extract_mainboard(d) == ["Real"]


def test_mainboard_empty_categories_counts():
    assert extract_mainboard(detail([card("Sol Ring", categories=[])])) \
        == ["Sol Ring"]


# ---------------------------------------------------------------------------
# fetch_top_decks
# ---------------------------------------------------------------------------

def make_fetcher(search_results, details, fail_ids=()):
    calls = []

    def fetch(url):
        calls.append(url)
        if "/decks/v3/" in url:
            return {"count": len(search_results),
                    "results": search_results}
        deck_id = int(url.rstrip("/").rsplit("/", 1)[1])
        if deck_id in fail_ids:
            raise OSError("boom")
        return details[deck_id]

    fetch.calls = calls
    return fetch


def test_fetch_top_decks_returns_name_lists():
    fetch = make_fetcher(
        [search_hit(1), search_hit(2)],
        {1: detail([card("A"), card("B")]), 2: detail([card("C")])},
    )
    out = fetch_top_decks("Krenko, Mob Boss", n=2, fetch_json=fetch)
    assert out == [["A", "B"], ["C"]]
    assert "commanderName=Krenko" in fetch.calls[0]


def test_fetch_top_decks_soft_bracket_filter():
    fetch = make_fetcher(
        [search_hit(1, edh_bracket=5), search_hit(2, edh_bracket=3),
         search_hit(3, edh_bracket=None)],
        {2: detail([card("B")]), 3: detail([card("C")])},
    )
    out = fetch_top_decks("X", bracket=3, n=5, fetch_json=fetch)
    # bracket-5 deck skipped without a fetch; null bracket passes.
    assert out == [["B"], ["C"]]
    assert not any("/decks/1/" in u for u in fetch.calls)


def test_fetch_top_decks_skips_partial_decks_and_failures():
    fetch = make_fetcher(
        [search_hit(1, size=30), search_hit(2), search_hit(3)],
        {3: detail([card("C")])},
        fail_ids={2},
    )
    out = fetch_top_decks("X", n=5, fetch_json=fetch)
    assert out == [["C"]]


def test_fetch_top_decks_stops_at_n():
    fetch = make_fetcher(
        [search_hit(i) for i in range(1, 6)],
        {i: detail([card(f"C{i}")]) for i in range(1, 6)},
    )
    out = fetch_top_decks("X", n=2, fetch_json=fetch)
    assert len(out) == 2
    # 1 search + exactly 2 detail fetches — no wasted requests.
    assert len(fetch.calls) == 3


def test_fetch_top_decks_search_failure_returns_empty():
    def fetch(url):
        raise OSError("down")
    assert fetch_top_decks("X", n=3, fetch_json=fetch) == []


def test_fetch_top_decks_n_zero_no_requests():
    fetch = make_fetcher([], {})
    assert fetch_top_decks("X", n=0, fetch_json=fetch) == []
    assert fetch.calls == []


def test_default_n_is_modest():
    # The corpus build's request budget — a deliberate cap, pinned so a
    # future edit can't silently 4x the cold-build fetch count.
    assert DEFAULT_N <= 30
