"""game_changers tests — fetch path mocked; fallback list verified."""
import json
from pathlib import Path

import pytest

from commander_builder.game_changers import (
    _FALLBACK,
    _MIN_SCRAPED_NAMES,
    _parse_card_names_from_html,
    _scrape_is_trustworthy,
    fetch_game_changers,
    is_game_changer,
    load_game_changers,
)


def _scrape_html(names) -> str:
    """Minimal WotC-page-shaped HTML carrying ``names`` as <li> items."""
    items = "".join(f"<li>{n}</li>" for n in names)
    return f"<html><body><main><ul>{items}</ul></main></body></html>"


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
    # The cache body has to be a *trustworthy* list or the whole entry is
    # rejected before the per-name filter matters (see
    # test_untrusted_cache_is_not_served), so pollute a real one.
    polluted_cache.write_text(json.dumps({
        "cards": sorted(_FALLBACK) + [
            "Banned & Restricted List",      # has & -> filtered
            "Some sentence: with colon",     # has : -> filtered
            "Sol Ring",                      # legitimate
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
        "cards": sorted(_FALLBACK) + ["Cached Card"],
        "scraped_count": len(_FALLBACK) + 1,
        "fallback_count": len(_FALLBACK),
    }), encoding="utf-8")
    monkeypatch.setattr("commander_builder.game_changers.CACHE_PATH", cache_file)

    def fail_fetch(url, timeout=None):
        raise AssertionError(f"should not have hit network: {url}")
    monkeypatch.setattr("commander_builder.game_changers._http_get_text", fail_fetch)

    cards = fetch_game_changers()
    assert "Cached Card" in cards
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
        lambda url, timeout=None: _scrape_html(
            sorted(_FALLBACK) + ["Surprise New Card"]),
    )
    cards = fetch_game_changers()
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "Surprise New Card" in data["cards"]
    # The scrape carried the whole list, so the fallback names persist too.
    assert all(name in data["cards"] for name in _FALLBACK)
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


def test_is_game_changer_is_case_insensitive(monkeypatch):
    """Every other card-name membership check in the codebase folds case
    (deck files / user input / EDHREC disagree on capitalization); the GC
    lookup must too — an exact-case check silently returned False for
    e.g. 'smothering tithe'."""
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Cyclonic Rift", "Smothering Tithe"},
    )
    assert is_game_changer("cyclonic rift") is True
    assert is_game_changer("SMOTHERING TITHE") is True
    assert is_game_changer("CyClOnIc RiFt") is True
    assert is_game_changer("sol ring") is False


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


# --------------------------------------------------------------------------- #
# Merge policy: a trusted scrape REPLACES the bundled fallback
#
# The old merge returned ``scraped | _FALLBACK``. A union can only ever grow,
# so a card WotC REMOVED from the Game Changers list stayed on ours forever
# and kept flooring innocent decks to B3. These pin the replace semantics and
# the sanity gate that makes destructive replacement safe.
# --------------------------------------------------------------------------- #

def test_wotc_url_points_at_the_maintained_format_page():
    """The beta ANNOUNCEMENT post is frozen — it still carries the
    launch-era list and will never be edited again. The maintained list
    lives on the Commander format page."""
    from commander_builder.game_changers import WOTC_URL
    assert WOTC_URL == "https://magic.wizards.com/en/formats/commander"
    assert "announcements" not in WOTC_URL


def test_trusted_scrape_replaces_fallback_so_removals_stick(tmp_path, monkeypatch):
    """THE BUG THIS FIXES: WotC drops a card; the scrape omits it; the
    result must omit it too. Under the old union it came back forever."""
    from commander_builder import game_changers as gc
    monkeypatch.setattr(gc, "CACHE_PATH", tmp_path / "fresh.json")
    removed = "Braids, Cabal Minion"
    assert removed in _FALLBACK
    kept = sorted(set(_FALLBACK) - {removed})
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(kept + ["Brand New Banger"]),
    )

    cards = gc.fetch_game_changers()
    assert removed not in cards          # the removal actually sticks
    assert "Brand New Banger" in cards   # and additions still land
    assert "Cyclonic Rift" in cards      # untouched entries survive


def test_trusted_scrape_logs_divergence_from_bundled_list(tmp_path, monkeypatch, capsys):
    """Divergence between a trusted scrape and _FALLBACK is the staleness
    alarm — the only signal a maintainer gets that WotC moved and the
    bundled list + audit prompt need re-syncing. It must be loud."""
    from commander_builder import game_changers as gc
    monkeypatch.setattr(gc, "CACHE_PATH", tmp_path / "fresh.json")
    kept = sorted(set(_FALLBACK) - {"Braids, Cabal Minion"})
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(kept + ["Brand New Banger"]),
    )

    gc.fetch_game_changers()
    out = capsys.readouterr().out
    assert "[game_changers]" in out
    assert "Brand New Banger" in out          # reported as ADDED
    assert "Braids, Cabal Minion" in out      # reported as REMOVED


def test_identical_scrape_logs_nothing(tmp_path, monkeypatch, capsys):
    """No divergence, no alarm — the steady state must stay quiet or the
    real alarm gets ignored."""
    from commander_builder import game_changers as gc
    monkeypatch.setattr(gc, "CACHE_PATH", tmp_path / "fresh.json")
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(sorted(_FALLBACK)),
    )
    assert gc.fetch_game_changers() == set(_FALLBACK)
    assert capsys.readouterr().out == ""


def test_short_parse_falls_back_wholesale_and_is_not_cached(tmp_path, monkeypatch):
    """A parse that yields a handful of names found a redesigned page or an
    error page, not the list. Fall back WHOLESALE (not union — a 3-name
    union is indistinguishable from the fallback anyway) and do NOT poison
    the cache with it for the full TTL."""
    from commander_builder import game_changers as gc
    cache = tmp_path / "fresh.json"
    monkeypatch.setattr(gc, "CACHE_PATH", cache)
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(["Rhystic Study", "Cyclonic Rift"]),
    )

    assert gc.fetch_game_changers() == set(_FALLBACK)
    assert not cache.exists()


def test_garbage_parse_falls_back_wholesale_and_is_not_cached(tmp_path, monkeypatch):
    """Enough names to clear the count bar, but none of them are ours —
    that is a different page (or a broken parser), not a WotC revision."""
    from commander_builder import game_changers as gc
    cache = tmp_path / "fresh.json"
    monkeypatch.setattr(gc, "CACHE_PATH", cache)
    junk = [f"Bogus Entry {i}" for i in range(_MIN_SCRAPED_NAMES + 10)]
    monkeypatch.setattr(
        gc, "_http_get_text", lambda url, timeout=None: _scrape_html(junk),
    )

    result = gc.fetch_game_changers()
    assert result == set(_FALLBACK)
    assert "Bogus Entry 0" not in result
    assert not cache.exists()


def test_rejected_scrape_is_logged(tmp_path, monkeypatch, capsys):
    """"We parsed something and it wasn't the list" needs a human; "the
    network was down" does not. Only the former prints."""
    from commander_builder import game_changers as gc
    monkeypatch.setattr(gc, "CACHE_PATH", tmp_path / "fresh.json")
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(["Rhystic Study"]),
    )
    gc.fetch_game_changers()
    assert "rejecting scrape" in capsys.readouterr().out

    import urllib.error
    def _down(url, timeout=None):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(gc, "_http_get_text", _down)
    gc.fetch_game_changers()
    assert capsys.readouterr().out == ""


def test_untrusted_cache_is_not_served(tmp_path, monkeypatch):
    """A cache entry is a persisted scrape and faces the same trust bar —
    it is returned INSTEAD of the fallback, so a truncated/hand-edited one
    must not be honored. It falls through to a live re-fetch."""
    from commander_builder import game_changers as gc
    cache = tmp_path / "gc.v2.json"
    cache.write_text(json.dumps({"cards": ["Rhystic Study"]}), encoding="utf-8")
    monkeypatch.setattr(gc, "CACHE_PATH", cache)
    monkeypatch.setattr(gc, "_cache_is_fresh", lambda p: True)
    monkeypatch.setattr(
        gc, "_http_get_text",
        lambda url, timeout=None: _scrape_html(sorted(_FALLBACK) + ["Refetched"]),
    )

    cards = gc.fetch_game_changers(use_cache=True)
    assert "Refetched" in cards  # the stale cache did not short-circuit us


def test_trust_gate_thresholds():
    """The gate itself: count bar AND overlap bar, both required."""
    fallback = sorted(_FALLBACK)
    # Real list, unchanged -> trusted, 100% overlap.
    trusted, overlap = _scrape_is_trustworthy(set(fallback))
    assert trusted and overlap == 1.0
    # Too few names -> rejected regardless of overlap quality.
    trusted, _ = _scrape_is_trustworthy(set(fallback[:_MIN_SCRAPED_NAMES - 1]))
    assert not trusted
    # Enough names, none of them ours -> rejected on overlap.
    trusted, overlap = _scrape_is_trustworthy(
        {f"Bogus {i}" for i in range(_MIN_SCRAPED_NAMES + 10)})
    assert not trusted and overlap == 0.0
    # Additions must NOT count against a scrape (overlap is fallback
    # coverage, not Jaccard).
    trusted, overlap = _scrape_is_trustworthy(
        set(fallback) | {f"New Card {i}" for i in range(50)})
    assert trusted and overlap == 1.0
