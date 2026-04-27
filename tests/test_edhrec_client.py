"""edhrec_client tests — offline-only.

Network calls are mocked; HTML parsing logic is exercised against
hand-crafted next-data blobs.
"""
import json
from pathlib import Path

import pytest

from commander_builder.edhrec_client import (
    CardEntry,
    CommanderPage,
    _extract_next_data,
    _is_cache_fresh,
    _page_from_dict,
    _parse_commander_page,
    _walk_for_cardlists,
    commander_slug,
    fetch_commander_page,
)


# --- commander_slug --------------------------------------------------------

def test_commander_slug_handles_apostrophes_and_commas():
    assert commander_slug("Atraxa, Praetors' Voice") == "atraxa-praetors-voice"


def test_commander_slug_lowercase():
    assert commander_slug("Edgar Markov") == "edgar-markov"


def test_commander_slug_collapses_whitespace_and_strips_punctuation():
    assert commander_slug("Krark, the Thumbless") == "krark-the-thumbless"


def test_commander_slug_empty_returns_unknown():
    assert commander_slug("...") == "unknown"


# --- _walk_for_cardlists ---------------------------------------------------

def test_walk_for_cardlists_buckets_by_header():
    blob = {
        "container": {
            "json_dict": {
                "cardlists": [
                    {
                        "header": "Top Cards",
                        "cardviews": [
                            {"name": "Sol Ring", "inclusion": 95.0, "synergy": 0.05, "num_decks": 1000},
                            {"name": "Arcane Signet", "inclusion": 80.0},
                        ],
                    },
                    {
                        "header": "High Synergy Cards",
                        "cardviews": [
                            {"name": "Tergrid", "inclusion": 60.0, "synergy": 0.55, "num_decks": 200},
                        ],
                    },
                    {
                        "header": "New Cards",
                        "cardviews": [
                            {"name": "Recent Card", "inclusion": 12.0},
                        ],
                    },
                ],
            }
        }
    }
    out: dict = {}
    _walk_for_cardlists(blob, out)
    assert len(out["top_cards"]) == 2
    assert out["top_cards"][0].name == "Sol Ring"
    assert out["top_cards"][0].inclusion_pct == 95.0
    assert out["top_cards"][0].num_decks == 1000
    # Synergy is multiplied by 100 so it scales the same as inclusion.
    assert out["high_synergy"][0].synergy_pct == pytest.approx(55.0)
    assert out["new_cards"][0].name == "Recent Card"


def test_walk_for_cardlists_handles_missing_fields():
    """Tolerant of EDHREC schema shifts — a malformed entry shouldn't crash
    the whole walk."""
    blob = {
        "header": "Top Cards",
        "cardviews": [
            {"name": "Foo"},                     # missing inclusion/synergy
            {"sanitized": "bar-card"},           # uses sanitized instead of name
            "not-a-dict",                         # garbage
        ],
    }
    out: dict = {}
    _walk_for_cardlists(blob, out)
    assert len(out["top_cards"]) == 2
    assert out["top_cards"][0].name == "Foo"
    assert out["top_cards"][1].name == "bar-card"


def test_walk_for_cardlists_recurses_through_nested_structure():
    blob = {
        "props": {
            "pageProps": {
                "data": {
                    "container": {
                        "header": "Top Cards",
                        "cardviews": [{"name": "Deeply Nested Card", "inclusion": 30.0}],
                    },
                },
            },
        },
    }
    out: dict = {}
    _walk_for_cardlists(blob, out)
    assert out["top_cards"][0].name == "Deeply Nested Card"


# --- _extract_next_data ----------------------------------------------------

def test_extract_next_data_parses_embedded_blob():
    html = '''
    <html>
    <body>
    <script id="__NEXT_DATA__" type="application/json">{"foo": "bar"}</script>
    </body>
    </html>
    '''
    assert _extract_next_data(html) == {"foo": "bar"}


def test_extract_next_data_raises_when_missing():
    with pytest.raises(ValueError, match="not found"):
        _extract_next_data("<html><body>no script here</body></html>")


def test_extract_next_data_raises_on_invalid_json():
    html = '<script id="__NEXT_DATA__" type="application/json">{ broken</script>'
    with pytest.raises(ValueError, match="isn't valid JSON"):
        _extract_next_data(html)


# --- _parse_commander_page -------------------------------------------------

def test_parse_commander_page_extracts_sections():
    next_data = {
        "props": {
            "pageProps": {
                "data": {
                    "deck_count": 12345,
                    "cardlists": [
                        {
                            "header": "Top Cards",
                            "cardviews": [
                                {"name": "Sol Ring", "inclusion": 95.0},
                            ],
                        },
                    ],
                },
            },
        },
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data) +
        '</script>'
    )
    page = _parse_commander_page("Test Commander", "test-commander", html)
    assert page.commander_name == "Test Commander"
    assert page.slug == "test-commander"
    assert page.deck_count == 12345
    assert len(page.top_cards) == 1
    assert page.top_cards[0].name == "Sol Ring"


def test_parse_commander_page_returns_empty_on_missing_blob():
    """A 404 or redirect that doesn't contain __NEXT_DATA__ should produce
    an empty page, not raise."""
    page = _parse_commander_page("Foo", "foo", "<html>oops</html>")
    assert page.top_cards == []
    assert page.deck_count is None


# --- cache helpers + fetch (with mocked HTTP) ------------------------------

def test_is_cache_fresh_within_ttl(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{}")
    assert _is_cache_fresh(p, ttl_hours=24)


def test_is_cache_fresh_missing_file(tmp_path):
    assert not _is_cache_fresh(tmp_path / "ghost.json")


def test_fetch_commander_page_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    cached = CommanderPage(
        commander_name="Cached", slug="cached",
        fetched_at="2026-04-26T00:00:00",
        top_cards=[CardEntry(name="Cache Hit")],
    )
    (tmp_path / "cached.json").write_text(cached.to_json(), encoding="utf-8")

    def fail_fetch(url):
        raise AssertionError(f"should not have hit network: {url}")
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", fail_fetch,
    )

    page = fetch_commander_page("cached")
    assert page.top_cards[0].name == "Cache Hit"


def test_fetch_commander_page_writes_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    next_data = {
        "props": {"pageProps": {"data": {"deck_count": 42, "cardlists": []}}}
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data) + '</script>'
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text",
        lambda url: html,
    )

    page = fetch_commander_page("New Commander")
    assert page.deck_count == 42
    # File written.
    assert (tmp_path / "new-commander.json").exists()


# --- round-trip ------------------------------------------------------------

def test_page_from_dict_round_trip():
    original = CommanderPage(
        commander_name="Foo", slug="foo", fetched_at="x",
        top_cards=[CardEntry(name="Card", inclusion_pct=50.0)],
        deck_count=100,
    )
    rehydrated = _page_from_dict(json.loads(original.to_json()))
    assert rehydrated.commander_name == "Foo"
    assert rehydrated.top_cards[0].name == "Card"
    assert rehydrated.deck_count == 100


# --- fetch_average_deck (mocked HTTP) --------------------------------------

def _avg_deck_html(card_names: list[str]) -> str:
    """Build an EDHREC-style HTML response with __NEXT_DATA__ embedded."""
    next_data = {
        "props": {
            "pageProps": {
                "data": {
                    "container": {
                        "header": "Average Deck",
                        "cardviews": [
                            {"name": n, "num_decks": 1} for n in card_names
                        ],
                    },
                },
            },
        },
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data) + '</script>'
    )


def test_fetch_average_deck_builds_url_from_bracket(tmp_path, monkeypatch):
    from commander_builder.edhrec_client import (
        BRACKET_SLUG,
        CACHE_DIR,
        fetch_average_deck,
    )
    monkeypatch.setattr("commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0)

    captured_urls: list[str] = []

    def fake_get(url):
        captured_urls.append(url)
        return _avg_deck_html(["Hakbal of the Surging Soul", "Sol Ring", "Forest"])
    monkeypatch.setattr("commander_builder.edhrec_client._http_get_text", fake_get)

    deck = fetch_average_deck("Hakbal of the Surging Soul", bracket=3,
                              budget="expensive")
    assert deck is not None
    assert len(captured_urls) == 1
    # URL should match what the user's deck used.
    assert "/average-decks/hakbal-of-the-surging-soul/upgraded/expensive" in captured_urls[0]
    assert deck.bracket_slug == "upgraded"
    assert deck.budget_slug == "expensive"
    names = [c.name for c in deck.cards]
    assert "Sol Ring" in names


def test_fetch_average_deck_returns_none_on_404(tmp_path, monkeypatch):
    from commander_builder.edhrec_client import fetch_average_deck
    monkeypatch.setattr("commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0)

    import urllib.error
    def raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr("commander_builder.edhrec_client._http_get_text", raise_404)
    assert fetch_average_deck("Foo", bracket=3) is None


def test_fetch_average_deck_returns_none_when_blob_has_no_cards(tmp_path, monkeypatch):
    from commander_builder.edhrec_client import fetch_average_deck
    monkeypatch.setattr("commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0)

    empty_html = '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>'
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", lambda url: empty_html,
    )
    assert fetch_average_deck("Foo", bracket=3) is None


def test_fetch_average_deck_uses_direct_url_when_provided(tmp_path, monkeypatch):
    from commander_builder.edhrec_client import fetch_average_deck
    monkeypatch.setattr("commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0)

    captured: list[str] = []
    def fake_get(url):
        captured.append(url)
        return _avg_deck_html(["Card A", "Card B"])
    monkeypatch.setattr("commander_builder.edhrec_client._http_get_text", fake_get)

    deck = fetch_average_deck(
        "irrelevant",
        direct_url="https://edhrec.com/average-decks/foo/upgraded/expensive",
    )
    assert deck is not None
    # Direct URL was used, not constructed.
    assert captured[0] == "https://edhrec.com/average-decks/foo/upgraded/expensive"
    assert len(deck.cards) == 2


def test_average_deck_to_moxfield_shape_routes_commander(tmp_path, monkeypatch):
    """The commander entry in the cards list should land in [Commander],
    everything else in [Main]."""
    from commander_builder.edhrec_client import AverageDeck, CardEntry
    deck = AverageDeck(
        commander_name="Hakbal of the Surging Soul",
        slug="hakbal-of-the-surging-soul",
        url="x",
        bracket_slug="upgraded",
        budget_slug="expensive",
        cards=[
            CardEntry(name="Hakbal of the Surging Soul"),  # the commander
            CardEntry(name="Sol Ring"),
            CardEntry(name="Forest"),
        ],
    )
    payload = deck.to_moxfield_shape(bracket_int=3)
    assert payload["bracket"] == 3
    cmdrs = payload["boards"]["commanders"]["cards"]
    main = payload["boards"]["mainboard"]["cards"]
    assert len(cmdrs) == 1
    assert any(c["card"]["name"] == "Hakbal of the Surging Soul"
               for c in cmdrs.values())
    assert any(c["card"]["name"] == "Sol Ring" for c in main.values())
    assert any(c["card"]["name"] == "Forest" for c in main.values())
