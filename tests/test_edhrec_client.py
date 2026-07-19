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
    _http_get_text_with_retry,
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


def test_commander_slug_dfc_uses_front_face():
    """Double-faced commanders use only the front face on EDHREC.
    Without the front-face split, the slug would include both halves
    and EDHREC returns 404."""
    slug = commander_slug(
        "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel",
    )
    assert slug == "sephiroth-fabled-soldier"


def test_commander_slug_dfc_keeps_apostrophe_strip():
    slug = commander_slug("Brimaz, Blight of Oreskos // Brimaz, King of Oreskos")
    assert slug == "brimaz-blight-of-oreskos"


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
    # The page must carry at least one card: empty parses are
    # deliberately NOT cached (cache-poisoning guard — see
    # _page_has_card_signal), so this fixture includes a cardlist to
    # exercise the "valid page → cached" happy path.
    next_data = {
        "props": {"pageProps": {"data": {
            "deck_count": 42,
            "cardlists": [
                {"header": "Top Cards", "cardviews": [
                    {"name": "Sol Ring", "inclusion": 61.0, "synergy": 0.05},
                ]},
            ],
        }}}
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
    assert page.top_cards[0].name == "Sol Ring"
    # File written, with the card signal intact.
    cache_file = tmp_path / "new-commander.json"
    assert cache_file.exists()
    assert "Sol Ring" in cache_file.read_text(encoding="utf-8")


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


def test_fetch_salt_list_parses_salt_scores(tmp_path, monkeypatch):
    """``fetch_salt_list`` returns a dict mapping lowercased card
    name to a 0-5 salt score, parsed from EDHREC's
    ``label: "Salt Score: X.XX"`` annotation."""
    from commander_builder.edhrec_client import fetch_salt_list

    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache",
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )

    salt_html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"data": {"cardlist": {"header": "Salty",'
        ' "cardviews": ['
        '{"name": "Smothering Tithe", "label": "Salt Score: 2.58"},'
        '{"name": "Stasis", "label": "Salt Score: 3.06"},'
        '{"name": "Boring Card", "label": "no salt here"}'
        ']}}}}</script>'
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", lambda url: salt_html,
    )
    salt = fetch_salt_list()
    assert salt["stasis"] == 3.06
    assert salt["smothering tithe"] == 2.58
    # Cards without a "Salt Score:" label are skipped.
    assert "boring card" not in salt


def test_fetch_salt_list_returns_empty_dict_on_fetch_failure(
    tmp_path, monkeypatch,
):
    """Best-effort: HTTP errors must not break the audit."""
    from commander_builder.edhrec_client import fetch_salt_list

    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path / "cache",
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )

    # Simulate the failure with a REAL network exception type: since the
    # except-narrowing (fetch failures are caught as OSError, matching
    # what urllib actually raises), a RuntimeError would propagate — by
    # design, so programming errors can't masquerade as "EDHREC down".
    import urllib.error

    def boom(url):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", boom,
    )
    # URLError is retryable, so the retry loop's backoff sleeps fire —
    # neutralize them to keep the test instant.
    import commander_builder.edhrec_client as ec
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    assert fetch_salt_list() == {}


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


# --- _http_get_text_with_retry — backoff on transient failures ------------

def _make_http_error(code: int):
    """Construct a urllib HTTPError with a given status code.

    Pass an explicit empty BytesIO for ``fp`` so Python's HTTPError
    doesn't allocate its own SpooledTemporaryFile under the hood —
    that tempfile triggers a ResourceWarning at GC time when the
    exception is raised + caught without being explicitly closed
    (12 such warnings surfaced under -W default before this fix).
    """
    import io
    import urllib.error
    return urllib.error.HTTPError(
        url="https://edhrec.com/x", code=code, msg="boom",
        hdrs=None, fp=io.BytesIO(b""),
    )


def test_retry_succeeds_after_503_then_ok(monkeypatch):
    """503 then 200 — wrapper retries and returns the success body."""
    import commander_builder.edhrec_client as ec
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error(503)
        return "<html>ok</html>"

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", flaky)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    body = _http_get_text_with_retry(
        "https://edhrec.com/x", max_retries=3, base_delay=1.0,
    )
    assert body == "<html>ok</html>"
    assert calls["n"] == 2
    # One backoff slept between attempt 1 and attempt 2.
    assert sleeps == [1.0]


def test_retry_uses_exponential_backoff(monkeypatch):
    """Successive failures should sleep 1s, 2s, 4s."""
    import commander_builder.edhrec_client as ec

    def always_503(url):
        raise _make_http_error(503)

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", always_503)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _http_get_text_with_retry(
            "https://edhrec.com/x", max_retries=3, base_delay=1.0,
        )
    # 3 retries → 3 backoffs between 4 attempts.
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_does_not_retry_on_404(monkeypatch):
    """404 is deterministic — no retries."""
    import commander_builder.edhrec_client as ec
    calls = {"n": 0}

    def four_oh_four(url):
        calls["n"] += 1
        raise _make_http_error(404)

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", four_oh_four)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as info:
        _http_get_text_with_retry("https://edhrec.com/x", max_retries=3)
    assert info.value.code == 404
    assert calls["n"] == 1  # exactly one attempt
    assert sleeps == []


def test_retry_on_urlerror_then_succeeds(monkeypatch):
    """Network failure (URLError / timeout) is also retried."""
    import commander_builder.edhrec_client as ec
    import urllib.error
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("connection refused")
        return "<html>ok</html>"

    monkeypatch.setattr(ec, "_http_get_text", flaky)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)

    body = _http_get_text_with_retry(
        "https://edhrec.com/x", max_retries=3, base_delay=0.01,
    )
    assert body == "<html>ok</html>"
    assert calls["n"] == 2


def test_retry_does_not_retry_4xx_other_than_429(monkeypatch):
    """Client errors (400, 401, 403) are deterministic — don't retry."""
    import commander_builder.edhrec_client as ec
    import urllib.error
    calls = {"n": 0}

    def forbidden(url):
        calls["n"] += 1
        raise _make_http_error(403)

    monkeypatch.setattr(ec, "_http_get_text", forbidden)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError):
        _http_get_text_with_retry("https://edhrec.com/x", max_retries=3)
    assert calls["n"] == 1


def test_retry_retries_on_429_rate_limit(monkeypatch):
    """429 means EDHREC is rate-limiting us — back off and retry."""
    import commander_builder.edhrec_client as ec
    calls = {"n": 0}

    def rate_limited(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_http_error(429)
        return "<html>finally</html>"

    monkeypatch.setattr(ec, "_http_get_text", rate_limited)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)

    body = _http_get_text_with_retry("https://edhrec.com/x", max_retries=3)
    assert body == "<html>finally</html>"
    assert calls["n"] == 3


# --- fetch_commander_page — integration with retry -------------------------

def test_fetch_commander_page_recovers_from_transient_503(tmp_path, monkeypatch):
    """503 then valid HTML → page is returned, cache is written."""
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.time.sleep", lambda s: None,
    )
    # Card signal included so the page qualifies for the cache write
    # (empty parses are deliberately not cached).
    next_data = {
        "props": {"pageProps": {"data": {
            "deck_count": 7,
            "cardlists": [
                {"header": "Top Cards", "cardviews": [
                    {"name": "Lightning Greaves", "inclusion": 40.0},
                ]},
            ],
        }}}
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data) + '</script>'
    )
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error(503)
        return html
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", flaky,
    )

    page = fetch_commander_page("Sephiroth")
    assert page is not None
    assert page.deck_count == 7
    assert calls["n"] == 2
    assert (tmp_path / "sephiroth.json").exists()


def test_fetch_commander_page_returns_none_after_exhausted_retries(
    tmp_path, monkeypatch,
):
    """All 4 attempts fail with 503 → graceful None so the audit
    falls through to the no-EDHREC heuristic path."""
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.time.sleep", lambda s: None,
    )

    def always_503(url):
        raise _make_http_error(503)
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", always_503,
    )

    page = fetch_commander_page("Sephiroth")
    assert page is None


# --- Retry-After header handling + retry logging ---------------------------

def _make_http_error_with_headers(code: int, headers: dict):
    """HTTPError with a real headers dict (email.message-like .get()).

    Same BytesIO-as-fp trick as ``_make_http_error`` — avoids
    HTTPError's auto-allocated tempfile and the resulting
    ResourceWarning at GC.
    """
    import io
    import urllib.error
    from email.message import Message
    msg = Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError(
        url="https://edhrec.com/x", code=code, msg="boom",
        hdrs=msg, fp=io.BytesIO(b""),
    )


def test_retry_respects_retry_after_seconds(monkeypatch):
    """429 with `Retry-After: 5` → sleep 5s, not the exp-backoff 1s."""
    import commander_builder.edhrec_client as ec
    calls = {"n": 0}

    def rate_limited(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error_with_headers(429, {"Retry-After": "5"})
        return "<html>ok</html>"

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", rate_limited)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    body = _http_get_text_with_retry(
        "https://edhrec.com/x", max_retries=3, base_delay=1.0,
    )
    assert body == "<html>ok</html>"
    # First (and only) backoff respected the server's hint.
    assert sleeps == [5.0]


def test_retry_caps_retry_after(monkeypatch):
    """A server saying `Retry-After: 999` should not block the user
    for 16 minutes — clamp to the module-level cap."""
    import commander_builder.edhrec_client as ec
    from commander_builder.edhrec_client import MAX_RETRY_AFTER_SEC

    def always_429(url):
        raise _make_http_error_with_headers(429, {"Retry-After": "999"})

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", always_429)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _http_get_text_with_retry(
            "https://edhrec.com/x", max_retries=3, base_delay=1.0,
        )
    # All three retries were clamped to the cap.
    assert all(s <= MAX_RETRY_AFTER_SEC for s in sleeps)
    assert sleeps == [MAX_RETRY_AFTER_SEC] * 3


def test_retry_after_http_date_format(monkeypatch):
    """Retry-After can be an HTTP-date — should be parsed as 'wait until
    that timestamp', not as seconds."""
    import commander_builder.edhrec_client as ec
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from email.utils import format_datetime

    # 3 seconds from "now".
    target = _dt.now(_tz.utc) + _td(seconds=3)
    date_str = format_datetime(target, usegmt=True)
    calls = {"n": 0}

    def rate_limited(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error_with_headers(503, {"Retry-After": date_str})
        return "<html>ok</html>"

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", rate_limited)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    body = _http_get_text_with_retry(
        "https://edhrec.com/x", max_retries=3, base_delay=1.0,
    )
    assert body == "<html>ok</html>"
    # Should be ~3s (the Retry-After target). Lower bound deliberately
    # above the 1.0s exp-backoff default so a no-op implementation
    # cannot pass this test by coincidence.
    assert 2.0 <= sleeps[0] <= 4.0


def test_retry_after_malformed_falls_back_to_exp_backoff(monkeypatch):
    """Garbage Retry-After header → ignore it, use exp backoff."""
    import commander_builder.edhrec_client as ec

    def always_503(url):
        raise _make_http_error_with_headers(503, {"Retry-After": "not a number"})

    sleeps: list[float] = []
    monkeypatch.setattr(ec, "_http_get_text", always_503)
    monkeypatch.setattr(ec.time, "sleep", lambda s: sleeps.append(s))

    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _http_get_text_with_retry(
            "https://edhrec.com/x", max_retries=3, base_delay=1.0,
        )
    # Falls back to 1s, 2s, 4s.
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_logs_each_attempt(monkeypatch, capsys):
    """Every retry should emit a single-line log so the operator sees
    'EDHREC was 503, retried' rather than silent slowdowns."""
    import commander_builder.edhrec_client as ec
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_http_error(503)
        return "<html>ok</html>"

    monkeypatch.setattr(ec, "_http_get_text", flaky)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)

    _http_get_text_with_retry("https://edhrec.com/x", max_retries=3)

    out = capsys.readouterr().out
    # Two retries fired (attempts 1 and 2 failed, attempt 3 succeeded).
    assert out.count("[edhrec] retry") == 2
    # Each log line names the status that triggered the retry.
    assert "503" in out


def test_no_log_when_first_attempt_succeeds(monkeypatch, capsys):
    """Happy path stays quiet — log only fires on actual retries."""
    import commander_builder.edhrec_client as ec
    monkeypatch.setattr(ec, "_http_get_text", lambda url: "<html>ok</html>")
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)

    _http_get_text_with_retry("https://edhrec.com/x")
    assert "[edhrec] retry" not in capsys.readouterr().out


# --- empty-parse cache-poisoning guard --------------------------------------
# An HTTP-200 response with no __NEXT_DATA__ (CDN bot-challenge page,
# redirect interstitial, schema change) parses to an EMPTY CommanderPage.
# Caching that would silently zero out EDHREC signal for the whole 24h
# TTL — so empty parses must be returned WITHOUT being cached, loudly.

_CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body>Checking your browser before accessing edhrec.com</body></html>"
)


def _valid_page_html(card_name: str = "Sol Ring") -> str:
    """Minimal commander/tag page HTML that parses to a NON-empty page."""
    next_data = {
        "props": {"pageProps": {"data": {
            "deck_count": 5,
            "cardlists": [
                {"header": "Top Cards", "cardviews": [
                    {"name": card_name, "inclusion": 50.0},
                ]},
            ],
        }}}
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data) + '</script>'
    )


def test_fetch_commander_page_does_not_cache_challenge_page(
    tmp_path, monkeypatch, capsys,
):
    """200-OK challenge page → empty page returned (contract unchanged),
    NOTHING written to the cache, and a loud warning printed."""
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text",
        lambda url: _CHALLENGE_HTML,
    )

    page = fetch_commander_page("Vexing Commander")
    # Return contract unchanged: callers still get an (empty) page.
    assert page is not None
    assert page.top_cards == []
    assert page.category_lists == {}
    # The poison never reaches disk.
    assert not (tmp_path / "vexing-commander.json").exists()
    assert list(tmp_path.glob("*.json")) == []
    out = capsys.readouterr().out
    assert "[edhrec] WARNING" in out
    assert "vexing-commander" in out
    assert "NOT cached" in out


def test_fetch_commander_page_caches_after_empty_parse_recovery(
    tmp_path, monkeypatch, capsys,
):
    """The whole point of not caching the empty parse: the NEXT call
    re-fetches, and a now-valid page is parsed + cached normally."""
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    responses = [_CHALLENGE_HTML, _valid_page_html("Arcane Signet")]
    calls = {"n": 0}

    def sequenced(url):
        body = responses[calls["n"]]
        calls["n"] += 1
        return body
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", sequenced,
    )

    first = fetch_commander_page("Vexing Commander")
    assert first.top_cards == []
    assert not (tmp_path / "vexing-commander.json").exists()

    # Second call must hit the network again (nothing poisoned the
    # cache) and this time succeed + cache.
    second = fetch_commander_page("Vexing Commander")
    assert calls["n"] == 2
    assert second.top_cards[0].name == "Arcane Signet"
    assert (tmp_path / "vexing-commander.json").exists()

    # Third call is served from the fresh, VALID cache — no network.
    third = fetch_commander_page("Vexing Commander")
    assert calls["n"] == 2
    assert third.top_cards[0].name == "Arcane Signet"


def test_fetch_tag_page_does_not_cache_challenge_page(
    tmp_path, monkeypatch, capsys,
):
    """Same guard on the tag-page path (separate cache directory)."""
    from commander_builder.edhrec_client import fetch_tag_page
    # Tag cache lives at CACHE_DIR.parent / "edhrec_tag".
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path / "edhrec",
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text",
        lambda url: _CHALLENGE_HTML,
    )

    page = fetch_tag_page("dragons")
    assert page is not None
    assert page.top_cards == []
    assert not (tmp_path / "edhrec_tag" / "dragons.json").exists()
    out = capsys.readouterr().out
    assert "[edhrec] WARNING" in out
    assert "dragons" in out
    assert "NOT cached" in out


def test_fetch_tag_page_caches_valid_page_after_empty_parse(
    tmp_path, monkeypatch,
):
    """Tag path: empty parse not cached → next call re-fetches and a
    valid page caches normally."""
    from commander_builder.edhrec_client import fetch_tag_page
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path / "edhrec",
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    responses = [_CHALLENGE_HTML, _valid_page_html("Terror of the Peaks")]
    calls = {"n": 0}

    def sequenced(url):
        body = responses[calls["n"]]
        calls["n"] += 1
        return body
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text", sequenced,
    )

    first = fetch_tag_page("dragons")
    assert first.top_cards == []
    assert not (tmp_path / "edhrec_tag" / "dragons.json").exists()

    second = fetch_tag_page("dragons")
    assert calls["n"] == 2
    assert second.top_cards[0].name == "Terror of the Peaks"
    assert (tmp_path / "edhrec_tag" / "dragons.json").exists()


def test_fetch_salt_list_challenge_page_warns_and_does_not_cache(
    tmp_path, monkeypatch, capsys,
):
    """Salt path already refused to cache empty maps (the precedent for
    the guard); it must now also WARN instead of degrading silently."""
    from commander_builder.edhrec_client import fetch_salt_list
    monkeypatch.setattr(
        "commander_builder.edhrec_client.CACHE_DIR", tmp_path / "edhrec",
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.REQUEST_SLEEP_SEC", 0,
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client._http_get_text",
        lambda url: _CHALLENGE_HTML,
    )

    assert fetch_salt_list() == {}
    assert not (tmp_path / "edhrec_salt" / "top-salt.json").exists()
    out = capsys.readouterr().out
    assert "[edhrec] WARNING" in out
    assert "NOT cached" in out


# --- narrowed exception handling in fetch_* ---------------------------------

def test_fetch_commander_page_warns_on_network_failure(
    tmp_path, monkeypatch, capsys,
):
    """Network failures (URLError et al.) still degrade to None, but now
    leave a diagnosable warning with the exception repr."""
    import urllib.error
    import commander_builder.edhrec_client as ec
    monkeypatch.setattr(ec, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ec, "REQUEST_SLEEP_SEC", 0)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)  # retry backoff

    def dns_down(url):
        raise urllib.error.URLError("getaddrinfo failed")
    monkeypatch.setattr(ec, "_http_get_text", dns_down)

    assert fetch_commander_page("Sephiroth") is None
    out = capsys.readouterr().out
    assert "[edhrec] WARNING" in out
    assert "URLError" in out  # exception repr present


def test_fetch_commander_page_no_longer_swallows_programming_errors(
    tmp_path, monkeypatch,
):
    """The old blanket ``except Exception`` converted ANY bug into a
    silent None ("EDHREC unavailable"). After narrowing to OSError, a
    programming error (e.g. TypeError) propagates to the caller."""
    import commander_builder.edhrec_client as ec
    monkeypatch.setattr(ec, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ec, "REQUEST_SLEEP_SEC", 0)

    def buggy(url):
        raise TypeError("someone broke the fetch layer")
    monkeypatch.setattr(ec, "_http_get_text", buggy)

    with pytest.raises(TypeError):
        fetch_commander_page("Sephiroth")
