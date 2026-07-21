"""scryfall_client tests. Pure-offline — no network in unit tests.

The `lookup_card` HTTP call is exercised by integration runs; here we test the
deterministic helpers (color normalization, .dck commander parsing, cache hit
path) plus mocked-failure behavior.
"""
import json
from pathlib import Path

import pytest

from commander_builder.scryfall_client import (
    _parse_commander_names_from_dck,
    color_identity_for_commander,
    lookup_card,
    normalize_color_identity,
)


# --- normalize_color_identity ----------------------------------------------

def test_normalize_color_identity_orders_to_wubrg():
    assert normalize_color_identity(["G", "W", "U"]) == "WUG"
    assert normalize_color_identity(["b", "r"]) == "BR"


def test_normalize_color_identity_empty_for_colorless():
    assert normalize_color_identity([]) == ""


def test_normalize_color_identity_dedups():
    assert normalize_color_identity(["W", "W", "B"]) == "WB"


def test_normalize_color_identity_handles_garbage():
    # Non-string elements should be ignored, not crash.
    assert normalize_color_identity(["W", None, 5, "B"]) == "WB"


# --- _parse_commander_names_from_dck ---------------------------------------

def _write_dck(tmp_path, content: str) -> Path:
    p = tmp_path / "test.dck"
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_commander_names_basic(tmp_path):
    p = _write_dck(tmp_path, "\n".join([
        "[metadata]",
        "Name=Test",
        "[Commander]",
        "1 Atraxa, Praetors' Voice|CMM|1",
        "[Main]",
        "1 Sol Ring|CMM|1",
    ]))
    assert _parse_commander_names_from_dck(p) == ["Atraxa, Praetors' Voice"]


def test_parse_commander_names_handles_partner_pairs(tmp_path):
    p = _write_dck(tmp_path, "\n".join([
        "[Commander]",
        "1 Krark, the Thumbless|MH2|123",
        "1 Sakashima of a Thousand Faces|CMR|45",
        "[Main]",
        "1 Sol Ring",
    ]))
    names = _parse_commander_names_from_dck(p)
    assert names == ["Krark, the Thumbless", "Sakashima of a Thousand Faces"]


def test_parse_commander_names_no_set_suffix(tmp_path):
    p = _write_dck(tmp_path, "\n".join([
        "[Commander]",
        "1 Foo Commander",
    ]))
    assert _parse_commander_names_from_dck(p) == ["Foo Commander"]


def test_parse_commander_names_missing_section(tmp_path):
    p = _write_dck(tmp_path, "\n".join([
        "[Main]",
        "1 Sol Ring",
    ]))
    assert _parse_commander_names_from_dck(p) == []


def test_parse_commander_names_missing_file():
    assert _parse_commander_names_from_dck(Path("/does/not/exist.dck")) == []


# --- lookup_card cache hit (no network) ------------------------------------

def test_lookup_card_uses_cache(tmp_path, monkeypatch):
    # Pre-populate the cache and verify lookup_card doesn't try to fetch.
    monkeypatch.setattr("commander_builder.scryfall_client.CACHE_DIR", tmp_path)
    fake = {"name": "Foo", "color_identity": ["W", "B"]}
    (tmp_path / "foo.json").write_text('{"name": "Foo", "color_identity": ["W", "B"]}', encoding="utf-8")

    def fail_fetch(url):
        raise AssertionError(f"Should not have fetched {url}")
    monkeypatch.setattr("commander_builder.scryfall_client._http_get_json", fail_fetch)

    out = lookup_card("Foo")
    assert out == fake


def test_lookup_card_handles_404(tmp_path, monkeypatch):
    monkeypatch.setattr("commander_builder.scryfall_client.CACHE_DIR", tmp_path)
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)

    import urllib.error
    def raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr("commander_builder.scryfall_client._http_get_json", raise_404)

    assert lookup_card("Nonexistent Card") is None


def test_lookup_card_empty_name():
    assert lookup_card("") is None


# --- refresh_card (force-fetch, bypass cache) -----------------------------

def test_refresh_card_writes_fresh_snapshot(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import refresh_card

    monkeypatch.setattr("commander_builder.scryfall_client.CACHE_DIR", tmp_path)
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)
    # Even if a stale cache file exists, refresh_card should re-fetch.
    (tmp_path / "sol_ring.json").write_text(
        '{"name": "Sol Ring", "oracle_text": "STALE"}', encoding="utf-8",
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json",
        lambda url: {"name": "Sol Ring", "oracle_text": "FRESH"},
    )

    result = refresh_card("Sol Ring")
    assert result["oracle_text"] == "FRESH"
    on_disk = json.loads((tmp_path / "sol_ring.json").read_text(encoding="utf-8"))
    assert on_disk["oracle_text"] == "FRESH"


def test_refresh_card_returns_none_on_404(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import refresh_card
    import urllib.error

    monkeypatch.setattr("commander_builder.scryfall_client.CACHE_DIR", tmp_path)
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)

    def raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", raise_404
    )

    assert refresh_card("Nonexistent") is None
    # No file written on miss.
    assert not (tmp_path / "nonexistent.json").exists()


def test_refresh_card_empty_name():
    from commander_builder.scryfall_client import refresh_card
    assert refresh_card("") is None


# --- lookup_card_prints (all-printings cache) -------------------------------

def _full_printing(**over):
    """A Scryfall-shaped card object with extra fields the trim must drop."""
    base = {
        "name": "Counterspell",
        "set": "dsc",
        "set_name": "Duskmourn Commander",
        "set_type": "commander",
        "collector_number": "114",
        "border_color": "black",
        "oversized": False,
        "digital": False,
        "prices": {"usd": "3.88", "usd_foil": None, "usd_etched": None,
                   "eur": "2.89", "tix": "1.29"},
        "legalities": {"commander": "legal", "modern": "legal"},
        "oracle_text": "Counter target spell.",   # must be trimmed away
        "image_uris": {"png": "http://example/x.png"},  # ditto
    }
    base.update(over)
    return base


def test_lookup_card_prints_fetches_trims_and_caches(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import lookup_card_prints

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json",
        lambda url: {"data": [_full_printing()], "has_more": False},
    )

    out = lookup_card_prints("Counterspell")
    assert isinstance(out, list) and len(out) == 1
    p = out[0]
    # Pricing-relevant fields kept…
    assert p["set"] == "dsc"
    assert p["collector_number"] == "114"
    assert p["prices"]["usd"] == "3.88"
    assert p["legalities"] == {"commander": "legal"}
    # …bulk fields trimmed (cache size + documented downstream contract).
    assert "oracle_text" not in p
    assert "image_uris" not in p
    # Cached under a "printings" key so the format can grow metadata.
    on_disk = json.loads(
        (tmp_path / "counterspell.json").read_text(encoding="utf-8"),
    )
    assert on_disk["printings"] == out


def test_lookup_card_prints_uses_cache_without_network(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import lookup_card_prints

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )
    (tmp_path / "foo.json").write_text(
        '{"name": "Foo", "printings": [{"set": "abc"}]}', encoding="utf-8",
    )

    def fail_fetch(url):
        raise AssertionError(f"Should not have fetched {url}")
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", fail_fetch,
    )
    assert lookup_card_prints("Foo") == [{"set": "abc"}]


def test_lookup_card_prints_follows_pagination(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import lookup_card_prints

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)
    pages = {
        None: {"data": [_full_printing(set="aaa")], "has_more": True,
               "next_page": "http://page2"},
        "http://page2": {"data": [_full_printing(set="bbb")],
                         "has_more": False},
    }

    def fake_fetch(url):
        return pages["http://page2"] if url == "http://page2" else pages[None]
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", fake_fetch,
    )
    out = lookup_card_prints("Counterspell")
    assert [p["set"] for p in out] == ["aaa", "bbb"]


def test_lookup_card_prints_cache_only_never_fetches(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import lookup_card_prints

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )

    def fail_fetch(url):
        raise AssertionError(f"Should not have fetched {url}")
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", fail_fetch,
    )
    # Nothing cached + cache_only → None, no network attempt.
    assert lookup_card_prints("Uncached Card", cache_only=True) is None


def test_lookup_card_prints_returns_none_on_404(tmp_path, monkeypatch):
    from commander_builder.scryfall_client import lookup_card_prints
    import urllib.error

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)

    def raise_404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", raise_404,
    )
    assert lookup_card_prints("Nonexistent") is None
    # No cache file written on miss.
    assert not (tmp_path / "nonexistent.json").exists()


def test_lookup_card_prints_propagates_network_errors(tmp_path, monkeypatch):
    """URLError must NOT be swallowed — deck_pricing's offline circuit
    breaker needs to distinguish 'offline' from 'card doesn't exist'."""
    from commander_builder.scryfall_client import lookup_card_prints
    import urllib.error

    monkeypatch.setattr(
        "commander_builder.scryfall_client.PRINTS_CACHE_DIR", tmp_path,
    )
    monkeypatch.setattr("commander_builder.scryfall_client.REQUEST_SLEEP_SEC", 0)

    def raise_urlerror(url):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(
        "commander_builder.scryfall_client._http_get_json", raise_urlerror,
    )
    with pytest.raises(urllib.error.URLError):
        lookup_card_prints("Counterspell")


def test_lookup_card_prints_empty_name():
    from commander_builder.scryfall_client import lookup_card_prints
    assert lookup_card_prints("") is None


# --- color_identity_for_commander ------------------------------------------

def test_color_identity_for_commander_uses_scryfall(tmp_path, monkeypatch):
    p = _write_dck(tmp_path, "\n".join([
        "[Commander]",
        "1 Atraxa, Praetors' Voice",
        "[Main]",
        "1 Sol Ring",
    ]))
    monkeypatch.setattr("commander_builder.scryfall_client.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {"color_identity": ["W", "U", "B", "G"]} if "Atraxa" in name else None,
    )
    assert color_identity_for_commander(p) == "WUBG"


def test_color_identity_merges_partner_pair(tmp_path, monkeypatch):
    p = _write_dck(tmp_path, "\n".join([
        "[Commander]",
        "1 Krark, the Thumbless",     # red
        "1 Sakashima of a Thousand Faces",   # blue
    ]))
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {
            "Krark, the Thumbless": {"color_identity": ["R"]},
            "Sakashima of a Thousand Faces": {"color_identity": ["U"]},
        }.get(name),
    )
    assert color_identity_for_commander(p) == "UR"


def test_color_identity_for_colorless_commander(tmp_path, monkeypatch):
    p = _write_dck(tmp_path, "[Commander]\n1 Kozilek, the Great Distortion")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {"color_identity": []},
    )
    assert color_identity_for_commander(p) == ""


def test_color_identity_when_lookup_fails(tmp_path, monkeypatch):
    p = _write_dck(tmp_path, "[Commander]\n1 Mystery Commander")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: None,
    )
    assert color_identity_for_commander(p) == ""


# ---------------------------------------------------------------------------
# format_card_for_display + diff_oracle_text — FP-009 parity API
# ---------------------------------------------------------------------------

def test_format_card_for_display_renders_known_card(monkeypatch):
    from commander_builder.scryfall_client import format_card_for_display
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {
            "name": "Lightning Bolt",
            "mana_cost": "{R}",
            "type_line": "Instant",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
            "cmc": 1.0,
            "color_identity": ["R"],
        },
    )
    text = format_card_for_display("Lightning Bolt")
    assert "Lightning Bolt" in text
    assert "{R}" in text
    assert "Instant" in text
    assert "deals 3 damage" in text
    assert "Color identity: R" in text
    assert "CMC: 1" in text


def test_format_card_for_display_returns_empty_when_unknown(monkeypatch):
    from commander_builder.scryfall_client import format_card_for_display
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: None,
    )
    assert format_card_for_display("Made Up") == ""


def test_format_card_for_display_includes_pt_for_creatures(monkeypatch):
    from commander_builder.scryfall_client import format_card_for_display
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {
            "name": "Llanowar Elves", "mana_cost": "{G}",
            "type_line": "Creature - Elf Druid",
            "oracle_text": "{T}: Add {G}.",
            "cmc": 1.0, "color_identity": ["G"],
            "power": 1, "toughness": 1,
        },
    )
    assert "1/1" in format_card_for_display("Llanowar Elves")


def test_format_card_for_display_handles_empty_name(monkeypatch):
    from commander_builder.scryfall_client import format_card_for_display
    assert format_card_for_display("") == ""


def test_diff_oracle_text_detects_change(monkeypatch):
    from commander_builder.scryfall_client import diff_oracle_text
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {"name": "Bolt", "oracle_text": "Old text."},
    )
    diff = diff_oracle_text("Bolt", "New text.")
    assert diff is not None
    assert diff["changed"] is True
    assert diff["before"] == "Old text."
    assert diff["after"] == "New text."


def test_diff_oracle_text_reports_no_change(monkeypatch):
    from commander_builder.scryfall_client import diff_oracle_text
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {"name": "Bolt", "oracle_text": "Same."},
    )
    assert diff_oracle_text("Bolt", "Same.")["changed"] is False


def test_diff_oracle_text_returns_none_when_unknown(monkeypatch):
    from commander_builder.scryfall_client import diff_oracle_text
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: None,
    )
    assert diff_oracle_text("Mystery", "anything") is None
