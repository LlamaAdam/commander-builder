"""Tests for archidekt_client — fully offline via injected fetch_json."""

from __future__ import annotations

import email.message
import urllib.error

import pytest

import commander_builder.archidekt_client as ac
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


def test_bad_edh_bracket_drops_hit_not_source():
    # API drift: a non-numeric edhBracket must cost that hit only —
    # before the guard it raised ValueError and sank the whole source.
    fetch = make_fetcher(
        [search_hit(1, edh_bracket="not-a-number"),
         search_hit(2, edh_bracket=3)],
        {2: detail([card("B")])},
    )
    out = fetch_top_decks("X", bracket=3, n=5, fetch_json=fetch)
    assert out == [["B"]]
    assert not any("/decks/1/" in u for u in fetch.calls)


def test_bad_edh_bracket_ignored_when_no_bracket_filter():
    # Without a bracket filter the field is never consulted, so drift
    # there must not cost the hit.
    fetch = make_fetcher([search_hit(1, edh_bracket="junk")],
                         {1: detail([card("A")])})
    assert fetch_top_decks("X", n=5, fetch_json=fetch) == [["A"]]


# ---------------------------------------------------------------------------
# 429 / transient retry
# ---------------------------------------------------------------------------

def http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", hdrs, None)


def test_search_429_retried_honoring_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ac.time, "sleep", sleeps.append)
    state = {"n": 0}

    def fetch(url):
        if "/decks/v3/" in url:
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(429, retry_after="7")
            return {"count": 1, "results": [search_hit(1)]}
        return detail([card("A")])

    out = fetch_top_decks("X", n=1, fetch_json=fetch)
    assert out == [["A"]]
    assert sleeps == [7.0]  # server hint honored, not the exp curve


def test_429_exhausted_returns_empty_source(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ac.time, "sleep", sleeps.append)
    calls = []

    def fetch(url):
        calls.append(url)
        raise http_error(429)

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == []
    assert len(calls) == ac.MAX_RETRIES + 1
    # No Retry-After header -> exponential fallback, base * 2^attempt.
    assert sleeps == [ac.RETRY_BASE_DELAY_SEC * (2 ** a)
                      for a in range(ac.MAX_RETRIES)]


def test_detail_429_retried_per_deck(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    state = {"n": 0}

    def fetch(url):
        if "/decks/v3/" in url:
            return {"count": 1, "results": [search_hit(1)]}
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(503)
        return detail([card("A")])

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == [["A"]]


def test_deterministic_4xx_not_retried(monkeypatch):
    monkeypatch.setattr(
        ac.time, "sleep",
        lambda s: pytest.fail("must not back off on a deterministic 4xx"))
    calls = []

    def fetch(url):
        calls.append(url)
        raise http_error(404)

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Single-deck import lane (decision C3) — the Moxfield fallback client
# ---------------------------------------------------------------------------

def _detail_card(name, quantity=1, categories=(), set_code="cmr", cn="123"):
    """An Archidekt detail-JSON card entry, including printing fields."""
    return {
        "quantity": quantity,
        "categories": list(categories),
        "card": {
            "oracleCard": {"name": name},
            "edition": {"editioncode": set_code},
            "collectorNumber": cn,
        },
    }


def _deck_detail(**over):
    d = {
        "id": 1234567,
        "name": "Krenko Goblins",
        "edhBracket": 3,
        "cards": [
            _detail_card("Krenko, Mob Boss", categories=["Commander"]),
            _detail_card("Sol Ring", set_code="c21", cn="7"),
            _detail_card("Mountain", quantity=27, set_code="", cn=""),
            _detail_card("Wishlist Card", categories=["Maybeboard"]),
        ],
        "categories": [
            {"name": "Commander", "includedInDeck": True},
            {"name": "Maybeboard", "includedInDeck": False},
        ],
    }
    d.update(over)
    return d


def test_is_archidekt_url_matches_host_only():
    assert ac.is_archidekt_url("https://archidekt.com/decks/1234567/krenko")
    assert ac.is_archidekt_url("https://www.ARCHIDEKT.com/decks/1")
    # A bare id is NOT claimed: that is exactly what a Moxfield call site
    # passes, and source selection is the caller's decision.
    assert not ac.is_archidekt_url("1234567")
    assert not ac.is_archidekt_url("https://moxfield.com/decks/abc123")
    assert not ac.is_archidekt_url("")


def test_parse_deck_id_from_url_and_bare_id():
    assert ac.parse_deck_id(
        "https://archidekt.com/decks/1234567/krenko-goblins") == "1234567"
    assert ac.parse_deck_id("https://archidekt.com/decks/1234567") == "1234567"
    assert ac.parse_deck_id("1234567") == "1234567"
    assert ac.parse_deck_id("  1234567  ") == "1234567"


def test_fetch_deck_hits_the_detail_endpoint():
    seen = []

    def fake_get(url):
        seen.append(url)
        return _deck_detail()

    out = ac.fetch_deck("https://archidekt.com/decks/1234567/krenko",
                        fetch_json=fake_get)
    assert seen == [f"{ac.BASE}/decks/1234567/"]
    assert out["name"] == "Krenko Goblins"


def test_fetch_deck_raises_unlike_the_corpus_functions():
    """Single-deck import has no redundancy to degrade into: swallowing
    the error would write an empty .dck and call it success."""
    def boom(url):
        raise urllib.error.URLError("dns")

    with pytest.raises(urllib.error.URLError):
        ac.fetch_deck("1234567", fetch_json=boom)


def test_fetch_deck_still_retries_transient_codes():
    calls = {"n": 0}
    hdrs = email.message.Message()

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 503, "busy", hdrs, None)
        return _deck_detail()

    out = ac.fetch_deck("1234567", fetch_json=flaky)
    assert calls["n"] == 2 and out["name"] == "Krenko Goblins"


@pytest.mark.parametrize("value,expected", [
    (3, 3), ("4", 4), (None, 0), ("weird", 0), (0, 0), (9, 0),
])
def test_deck_bracket_normalizes_to_the_moxfield_range(value, expected):
    """0 is the same 'unknown' resolve_bracket returns, so an unbracketed
    Archidekt deck lands as ``[B?]`` exactly like an unbracketed Moxfield
    one — most Archidekt decks leave edhBracket null."""
    assert ac.deck_bracket({"edhBracket": value}) == expected


def test_to_deck_json_splits_commander_from_mainboard():
    out = ac.to_deck_json(_deck_detail())
    cmd = [e["card"]["name"]
           for e in out["boards"]["commanders"]["cards"].values()]
    main = [e["card"]["name"]
            for e in out["boards"]["mainboard"]["cards"].values()]
    assert cmd == ["Krenko, Mob Boss"]
    # Maybeboard excluded (includedInDeck: false), commander not duplicated.
    assert main == ["Sol Ring", "Mountain"]
    assert out["format"] == "commander"
    assert out["name"] == "Krenko Goblins"
    assert out["bracket"] == 3


def test_to_deck_json_preserves_quantity_and_printing():
    """Quantities and set/collector number must survive: the .dck writer
    reads them to pin a printing, and a stacked ``27 Mountain`` line
    collapsing to 1x would silently rewrite the manabase."""
    out = ac.to_deck_json(_deck_detail())
    by_name = {e["card"]["name"]: e
               for e in out["boards"]["mainboard"]["cards"].values()}
    assert by_name["Mountain"]["quantity"] == 27
    assert by_name["Sol Ring"]["card"]["set"] == "c21"
    assert by_name["Sol Ring"]["card"]["cn"] == "7"


def test_to_deck_json_omits_bracket_when_unknown():
    """``resolve_bracket`` prefers its own 'unknown' default over an
    out-of-range int, so an unset bracket must not appear at all."""
    out = ac.to_deck_json(_deck_detail(edhBracket=None))
    assert "bracket" not in out


def test_to_deck_json_never_claims_a_moxfield_public_id():
    """An Archidekt id is not a Moxfield publicId; stamping one into the
    ``Moxfield=`` line would poison the re-import dedupe index."""
    assert "publicId" not in ac.to_deck_json(_deck_detail())


def test_to_deck_json_defaults_a_missing_name():
    out = ac.to_deck_json(_deck_detail(name=""))
    assert out["name"] == "Untitled"


def test_extract_mainboard_and_to_deck_json_agree_on_membership():
    """One walk, one notion of 'part of the deck' — the corpus reader and
    the importer must never read the same URL differently."""
    detail = _deck_detail()
    corpus = extract_mainboard(detail)
    imported = [e["card"]["name"] for e in
                ac.to_deck_json(detail)["boards"]["mainboard"]
                ["cards"].values()]
    assert corpus == imported
