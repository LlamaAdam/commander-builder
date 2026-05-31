"""game_changers tests — fetch path mocked; fallback list verified."""
import json
from pathlib import Path

import pytest

from commander_builder.game_changers import (
    _FALLBACK,
    _parse_card_names_from_html,
    fetch_game_changers,
    is_game_changer,
    load_game_changers,
)


def test_parser_strips_nav_header_footer_chrome():
    """Site-chrome <li> items in <nav>/<header>/<footer>/<aside> wrappers
    must NOT be returned as card names (the prior parser let "About",
    "Privacy Policy", "Wizards Play Network", etc. through).
    """
    html = """
    <html><body>
    <nav><ul>
        <li><a href="/about">About</a></li>
        <li><a href="/privacy">Privacy Policy</a></li>
        <li><a href="/wpn">Wizards Play Network</a></li>
    </ul></nav>
    <header><ul><li>Articles</li><li>Events</li></ul></header>
    <main>
        <ul>
            <li>Sol Ring</li>
            <li>Demonic Tutor</li>
            <li>Yawgmoth, Thran Physician</li>
        </ul>
    </main>
    <footer><ul><li>Terms</li><li>Sitemap</li></ul></footer>
    </body></html>
    """
    out = _parse_card_names_from_html(html)
    assert "Sol Ring" in out
    assert "Demonic Tutor" in out
    assert "Yawgmoth, Thran Physician" in out
    # Chrome must be stripped:
    for chrome in ("About", "Privacy Policy", "Wizards Play Network",
                   "Articles", "Events", "Terms", "Sitemap"):
        assert chrome not in out, f"chrome leaked: {chrome!r}"


def test_parser_decodes_html_entities_and_rejects_ampersand():
    """``&amp;`` must decode to ``&`` and then the entry must be rejected
    (no Magic card has ``&`` in its name -- this kills the
    "Banned &amp; Restricted List" entry the prior parser persisted)."""
    html = "<main><ul><li>Banned &amp; Restricted List</li><li>Sol Ring</li></ul></main>"
    out = _parse_card_names_from_html(html)
    assert "Sol Ring" in out
    assert "Banned & Restricted List" not in out
    assert "Banned &amp; Restricted List" not in out


def test_cache_path_is_versioned():
    """The cache filename is versioned (.v2.json) so files written by the
    prior over-permissive parser are orphaned and ignored on read -- the
    cleanest "invalidate polluted caches everywhere" mechanism."""
    from commander_builder.game_changers import CACHE_PATH
    assert CACHE_PATH.name == "game_changers.v2.json", (
        f"unversioned cache path would still read pre-fix files: {CACHE_PATH}"
    )


def test_load_filters_punctuation_chrome_from_cache(tmp_path, monkeypatch):
    """Defense in depth: even if a cache somehow contains entries with
    sentence punctuation or ampersands (e.g. "Banned & Restricted List"),
    the post-read filter strips them. (Single-word chrome like "About"
    cannot be filtered after the fact -- the parser + cache-version bump
    handle that on the write side.)
    """
    from commander_builder import game_changers as gc
    polluted_cache = tmp_path / "gc.v2.json"
    polluted_cache.write_text(json.dumps({
        "cards": [
            "Banned & Restricted List",      # has & -> filtered
            "Some sentence: with colon",     # has : -> filtered
            "Sol Ring", "Demonic Tutor",     # legitimate
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(gc, "CACHE_PATH", polluted_cache)
    monkeypatch.setattr(gc, "_cache_is_fresh", lambda p: True)
    out = fetch_game_changers(use_cache=True)
    assert "Sol Ring" in out
    assert "Demonic Tutor" in out
    assert "Banned & Restricted List" not in out
    assert "Some sentence: with colon" not in out


def test_fallback_includes_canonical_high_power_cards():
    """Sanity check the bundled list. Anything missing here would be a
    regression in audit prompt sync."""
    must_have = {
        "Smothering Tithe", "Cyclonic Rift", "Demonic Tutor",
        "Mana Crypt" if False else "Mana Vault", "Gaea's Cradle",
        "The One Ring", "Thassa's Oracle", "Underworld Breach",
    }
    assert must_have <= _FALLBACK


def test_parse_card_names_from_html_extracts_li_items():
    html = """
    <ul>
      <li>Smothering Tithe</li>
      <li>Cyclonic Rift</li>
      <li>This is a long sentence that shouldn't be matched as a card name.</li>
      <li>has a colon: not a card</li>
      <li>lowercase start</li>
    </ul>
    """
    names = _parse_card_names_from_html(html)
    assert "Smothering Tithe" in names
    assert "Cyclonic Rift" in names
    # Filtered out:
    assert not any("colon" in n for n in names)
    assert not any("lowercase" in n for n in names)
    assert not any("sentence" in n for n in names)


def test_parse_handles_html_entities_and_nested_tags():
    html = "<li><strong>Demonic Tutor</strong></li>"
    names = _parse_card_names_from_html(html)
    assert "Demonic Tutor" in names


def test_parse_skips_overlong_text():
    html = "<li>" + " ".join(["Word"] * 20) + "</li>"
    assert _parse_card_names_from_html(html) == set()


def test_fetch_game_changers_uses_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / "game_changers.json"
    cache_file.write_text(json.dumps({
        "fetched_at": "2026-04-26T00:00:00",
        "cards": ["Smothering Tithe", "Cached Card"],
        "scraped_count": 0,
        "fallback_count": 0,
    }), encoding="utf-8")
    monkeypatch.setattr("commander_builder.game_changers.CACHE_PATH", cache_file)

    def fail_fetch(url, timeout=None):
        raise AssertionError(f"should not have hit network: {url}")
    monkeypatch.setattr("commander_builder.game_changers._http_get_text", fail_fetch)

    cards = fetch_game_changers()
    assert "Cached Card" in cards
    # Fallback union'd in even when cache was used.
    assert "Cyclonic Rift" in cards


def test_fetch_game_changers_falls_back_on_network_error(tmp_path, monkeypatch):
    monkeypatch.setattr("commander_builder.game_changers.CACHE_PATH", tmp_path / "fresh.json")
    import urllib.error
    def network_down(url, timeout=None):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr("commander_builder.game_changers._http_get_text", network_down)

    cards = fetch_game_changers()
    # Fallback list is what we get back.
    assert cards == set(_FALLBACK)


def test_fetch_writes_cache_after_successful_fetch(tmp_path, monkeypatch):
    cache_file = tmp_path / "fresh.json"
    monkeypatch.setattr("commander_builder.game_changers.CACHE_PATH", cache_file)
    monkeypatch.setattr(
        "commander_builder.game_changers._http_get_text",
        lambda url, timeout=None: "<ul><li>Surprise New Card</li></ul>",
    )
    cards = fetch_game_changers()
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "Surprise New Card" in data["cards"]
    # Cache is the union, so fallback names are also persisted.
    assert any(name in data["cards"] for name in _FALLBACK)
    # And the in-memory return matches.
    assert "Surprise New Card" in cards


def test_load_game_changers_returns_fallback_on_outer_exception(monkeypatch):
    """The outer wrapper should never raise — even if `fetch` blows up,
    audits get a non-empty list."""
    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr("commander_builder.game_changers.fetch_game_changers", boom)
    cards = load_game_changers()
    assert cards == set(_FALLBACK)


def test_is_game_changer_lookup(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Cyclonic Rift", "Smothering Tithe"},
    )
    assert is_game_changer("Cyclonic Rift") is True
    assert is_game_changer("Sol Ring") is False


def test_failed_scrape_is_not_cached(tmp_path, monkeypatch):
    """A failed/empty WotC scrape must degrade to the fallback WITHOUT
    persisting the cache -- otherwise the fallback-only list would look
    "fresh" for the whole TTL and never retry."""
    from commander_builder import game_changers
    cache = tmp_path / "game_changers.json"
    monkeypatch.setattr(game_changers, "CACHE_PATH", cache)

    def _boom(url, *a, **kw):
        raise OSError("network down")
    monkeypatch.setattr(game_changers, "_http_get_text", _boom)

    result = game_changers.fetch_game_changers(use_cache=True)
    assert result == set(game_changers._FALLBACK)   # degrades to fallback
    assert not cache.exists()                        # but does NOT persist it
