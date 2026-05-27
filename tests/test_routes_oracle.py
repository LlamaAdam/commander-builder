"""Tests for the FP-009 ``/api/oracle/<card_name>`` endpoint.

Three things this module pins:

1. The endpoint returns the projected shape the audit-panel UI
   consumes (mana_cost, type_line, oracle_text, power/toughness/
   loyalty, cmc) — sourced from byte-exact Scryfall fixtures in
   ``tests/fixtures/real_oracles.py``. No synthetic oracle text;
   see that module's docstring for the rationale.
2. 404s are returned for unknown cards, 502s for upstream failures,
   400s for empty names. The UI relies on these status codes to
   distinguish "card doesn't exist" from "network blip".
3. The in-process projection cache holds a result for the configured
   TTL and the second hit DOES NOT call ``lookup_card`` again. This
   matters because Scryfall asks for ≥75ms between requests and
   we'd otherwise burn ~30 round-trips per audit panel render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app
from commander_builder.web import routes_oracle
from tests.fixtures.real_oracles import ORACLES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_deck(deck_dir: Path, name: str) -> Path:
    p = deck_dir / f"{name}.dck"
    p.write_text(
        "[metadata]\nName=stub\n\n[Commander]\n1 Atraxa, Praetors' Voice\n\n"
        "[Main]\n" + "1 Forest\n" * 35,
        encoding="utf-8",
    )
    return p


@pytest.fixture
def deck_dir(tmp_path):
    d = tmp_path / "decks"
    d.mkdir()
    _write_deck(d, "Stub")
    return d


@pytest.fixture
def app(deck_dir):
    # Drop the in-process projection cache between tests so a hit in
    # one test doesn't leak into another and mask a real bug.
    routes_oracle._clear_projection_cache()
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_fake_lookup(call_log: list[str]):
    """Return a fake ``lookup_card`` that:
      - returns a real-oracle fixture entry projected into the
        Scryfall response shape for any name in ORACLES,
      - returns synthetic full data for known commanders we test
        against (The First Sliver, Krenko, Mob Boss) — sourced from
        the live Scryfall API docs so the field shape matches what
        the real client returns,
      - returns ``None`` for unknown names (Scryfall 404).
    The ``call_log`` parameter records each invocation so cache tests
    can assert call counts without depending on global state.
    """
    # Curated full payloads for the two live-verification cards from
    # the task spec. These mirror the exact Scryfall field shape so
    # the projection logic gets exercised end-to-end. Values verified
    # against ``api.scryfall.com/cards/named?exact=...`` on 2026-05-15.
    live_cards = {
        "the first sliver": {
            "name": "The First Sliver",
            "mana_cost": "{W}{U}{B}{R}{G}",
            "type_line": "Legendary Creature — Sliver",
            "oracle_text": (
                "Cascade (When you cast this spell, exile cards from "
                "the top of your library until you exile a nonland "
                "card that costs less. You may cast it without paying "
                "its mana cost. Put the exiled cards on the bottom in "
                "a random order.)\n"
                "Sliver spells you cast have cascade."
            ),
            "power": "7",
            "toughness": "7",
            "loyalty": None,
            "cmc": 5.0,
            # Extra fields the projection should silently drop.
            "color_identity": ["W", "U", "B", "R", "G"],
            "prices": {"usd": "12.34"},
        },
        "krenko, mob boss": {
            "name": "Krenko, Mob Boss",
            "mana_cost": "{2}{R}{R}",
            "type_line": "Legendary Creature — Goblin Warrior",
            "oracle_text": (
                "{T}: Create X 1/1 red Goblin creature tokens, where "
                "X is the number of Goblins you control."
            ),
            "power": "3",
            "toughness": "3",
            "loyalty": None,
            "cmc": 4.0,
            "color_identity": ["R"],
            "prices": {"usd": "1.50"},
        },
    }

    def fake_lookup(name, cache=True):
        call_log.append(name)
        key = (name or "").strip().lower()
        if key in live_cards:
            return live_cards[key]
        # Map the real_oracles fixture into a Scryfall-shape dict.
        for fixture_name, oracle_data in ORACLES.items():
            if fixture_name.lower() == key:
                return {
                    "name": fixture_name,
                    "mana_cost": None,  # fixture doesn't carry mana_cost
                    "type_line": oracle_data["type_line"],
                    "oracle_text": oracle_data["oracle_text"],
                    "power": None,
                    "toughness": None,
                    "loyalty": None,
                    "cmc": None,
                }
        return None  # Scryfall 404
    return fake_lookup


# ---------------------------------------------------------------------------
# Happy path — live-verification cards from the task spec
# ---------------------------------------------------------------------------

def test_returns_projection_for_the_first_sliver(client, monkeypatch):
    """Live-verification case from FP-009: hover/click on 'The First
    Sliver' must surface its mana_cost, type_line, and full cascade
    oracle text. This is the exact UX the audit-panel tooltip relies
    on."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    resp = client.get("/api/oracle/The First Sliver")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "The First Sliver"
    assert body["mana_cost"] == "{W}{U}{B}{R}{G}"
    assert "Legendary Creature" in body["type_line"]
    assert "cascade" in body["oracle_text"].lower()
    # Power/toughness make it through for creatures.
    assert body["power"] == "7"
    assert body["toughness"] == "7"
    # Projection drops Scryfall fields the UI doesn't need.
    assert "color_identity" not in body
    assert "prices" not in body


def test_returns_projection_for_krenko_mob_boss(client, monkeypatch):
    """Second live-verification case from FP-009: hover should reveal
    Krenko's activated ability text. Pins the {T}: cost glyph + the
    'X is the number of Goblins you control' templating because real
    Scryfall data uses these literal glyphs (not 'Tap:' / 'X equals')."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    resp = client.get("/api/oracle/Krenko, Mob Boss")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Krenko, Mob Boss"
    assert body["mana_cost"] == "{2}{R}{R}"
    assert "{T}:" in body["oracle_text"]
    assert "Goblin" in body["oracle_text"]


# ---------------------------------------------------------------------------
# Real-oracle fixture coverage — ensures the projection round-trips
# every multi-paragraph + special-glyph case we already curate for the
# classifier tests. This is the regression net for "did the projection
# silently mangle Crux of Fate's bullet glyphs / Toxic Deluge's
# additional-cost paragraph break?"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("card_name", sorted(ORACLES.keys()))
def test_projection_preserves_real_oracle_text_byte_exact(
    card_name, client, monkeypatch,
):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    resp = client.get(f"/api/oracle/{card_name}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    # The projection MUST round-trip Scryfall's oracle text without
    # normalizing newlines, bullet glyphs, em-dashes, or whitespace.
    # The classifier tests already validated those glyphs are
    # essential; the UI tooltip is just rendering them, not parsing.
    assert body["oracle_text"] == ORACLES[card_name]["oracle_text"]
    assert body["type_line"] == ORACLES[card_name]["type_line"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_unknown_card_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    resp = client.get("/api/oracle/Definitely Not A Real Card 9000")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "card not found"
    # Name echoed back so the UI can include it in the error toast.
    assert "Definitely Not A Real Card 9000" in body["name"]


def test_upstream_failure_returns_502(client, monkeypatch):
    """When Scryfall is unreachable and no cached copy exists, surface
    a 502 so the UI can degrade gracefully (keep thumbnail working,
    show 'oracle unavailable' in the tooltip) instead of dying."""
    def explode(name, cache=True):
        raise ConnectionError("upstream went away")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", explode,
    )
    resp = client.get("/api/oracle/Anything")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["error"] == "oracle lookup failed"
    assert "ConnectionError" in body["detail"]


def test_empty_path_segment_returns_404(client):
    """Flask's path converter rejects a literally empty segment with
    a 404 from the router — we don't even reach our handler. This
    test pins that behavior so a refactor that adds an empty-name
    code path doesn't silently start hitting Scryfall with ''."""
    resp = client.get("/api/oracle/")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cache behavior — the core contract of the endpoint
# ---------------------------------------------------------------------------

def test_second_call_within_ttl_does_not_hit_lookup(client, monkeypatch):
    """Critical: caching means the second hit MUST NOT call
    ``lookup_card`` again. Without this, a 30-card audit panel
    re-rendering would burn 30 Scryfall round-trips per render and
    blow past Scryfall's 75ms-between-requests rate limit."""
    call_log: list[str] = []
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup(call_log),
    )

    # First call: miss → upstream lookup.
    resp1 = client.get("/api/oracle/The First Sliver")
    assert resp1.status_code == 200
    assert resp1.headers.get("X-Oracle-Cache") == "miss"
    assert call_log == ["The First Sliver"]

    # Second call: hit → no upstream call.
    resp2 = client.get("/api/oracle/The First Sliver")
    assert resp2.status_code == 200
    assert resp2.headers.get("X-Oracle-Cache") == "hit"
    # ``call_log`` unchanged from before — proves cache served us.
    assert call_log == ["The First Sliver"]

    # Both responses carry identical body (cache fidelity).
    assert resp1.get_json() == resp2.get_json()


def test_cache_is_case_insensitive(client, monkeypatch):
    """Audit panel uses the exact card name from the .dck file, but
    Moxfield exports and EDHREC scrape responses sometimes differ in
    casing ('lightning bolt' vs 'Lightning Bolt'). Cache key folds
    case so these collapse to one entry instead of doubling our
    Scryfall hit rate."""
    call_log: list[str] = []
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup(call_log),
    )

    client.get("/api/oracle/Krenko, Mob Boss")
    client.get("/api/oracle/krenko, mob boss")
    client.get("/api/oracle/KRENKO, MOB BOSS")
    # All three map to the same cache slot → exactly one upstream call.
    assert len(call_log) == 1


def test_cache_expiry_triggers_refetch(client, monkeypatch):
    """When the TTL elapses, the next call must re-fetch from
    Scryfall. Pinned because the eviction logic uses
    ``time.time()`` and an off-by-one TTL comparison would either
    serve stale data forever or never cache at all."""
    call_log: list[str] = []
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup(call_log),
    )

    # Pin time inside the cache module so we control expiry without
    # actually sleeping for an hour.
    fake_now = [1_000_000.0]
    monkeypatch.setattr(
        "commander_builder.web.routes_oracle.time.time",
        lambda: fake_now[0],
    )

    client.get("/api/oracle/Krenko, Mob Boss")
    assert len(call_log) == 1

    # Jump past the 1-hour TTL.
    fake_now[0] += 3601.0

    client.get("/api/oracle/Krenko, Mob Boss")
    # Cache expired → re-fetched.
    assert len(call_log) == 2


def test_unknown_card_is_not_cached(client, monkeypatch):
    """404s should NOT poison the cache — if a card was missing
    transiently (Scryfall data sync lag, typo the user just fixed),
    the next request should retry the lookup rather than serving a
    stale 404. We assert this by hitting 'Unknown' twice and
    counting upstream calls."""
    call_log: list[str] = []
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup(call_log),
    )

    r1 = client.get("/api/oracle/Definitely Unknown Card")
    r2 = client.get("/api/oracle/Definitely Unknown Card")
    assert r1.status_code == 404
    assert r2.status_code == 404
    # Both calls hit the upstream — no cached 404.
    assert len(call_log) == 2


# ---------------------------------------------------------------------------
# image_url — decorative image URL field (FP-008/FP-009 requirement)
# ---------------------------------------------------------------------------

def test_response_includes_image_url_field(client, monkeypatch):
    """Every oracle response MUST carry an ``image_url`` field — even
    if the client never renders it, the field contract must hold so
    callers don't need to special-case its absence.

    The URL routes through the local disk-cache route
    (``/api/card_image/normal/<name>``) rather than pointing directly
    at Scryfall, so the browser's card tooltip and image overlay both
    stay alive during a Scryfall outage.
    """
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    resp = client.get("/api/oracle/Krenko, Mob Boss")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "image_url" in body, "image_url must be present in the response"
    url = body["image_url"]
    assert url is not None
    assert url.startswith("/api/card_image/normal/")


def test_image_url_encodes_card_name(client, monkeypatch):
    """Card names with spaces, commas, and ``//`` separators must be
    URL-encoded in the image_url so the ``/api/card_image`` route
    receives an intact name via Flask's ``path:`` converter.

    Unencoded spaces become ``%20`` (not ``+``); ``//`` becomes
    ``%2F%2F``. Without encoding, ``Fire // Ice`` would produce a
    literal ``/`` in the path and get mis-routed by the WSGI layer.
    """
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    # Test with a space + comma in the name.
    resp = client.get("/api/oracle/Krenko, Mob Boss")
    body = resp.get_json()
    assert resp.status_code == 200
    # Comma and space should both be encoded.
    assert "Krenko" in body["image_url"]
    # Must not contain a raw space.
    assert " " not in body["image_url"]


def test_image_url_uses_canonical_scryfall_name(client, monkeypatch):
    """``image_url`` should use the card's canonical Scryfall name
    (the ``name`` field in the Scryfall response) rather than whatever
    casing the caller used.

    This matters because the disk-cache slug is derived from the name;
    using the canonical form means ``krenko, mob boss`` and
    ``Krenko, Mob Boss`` both produce the same URL and share the same
    cached image on disk.
    """
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _make_fake_lookup([]),
    )
    # Lowercase query — Scryfall response carries the properly-cased name.
    resp_lower = client.get("/api/oracle/krenko, mob boss")
    resp_proper = client.get("/api/oracle/Krenko, Mob Boss")
    assert resp_lower.status_code == 200
    assert resp_proper.status_code == 200
    # Both should encode to the same image_url (same canonical name).
    assert resp_lower.get_json()["image_url"] == resp_proper.get_json()["image_url"]


def test_image_url_for_split_card(client, monkeypatch):
    """Split/DFC card names containing ``//`` must survive the
    round-trip through URL encoding: the image_url should encode
    ``//`` as ``%2F%2F`` so the path stays unambiguous.
    """
    import urllib.parse

    # Build a minimal fake card whose name includes '//'.
    def _fake_lookup(name, cache=True):
        return {
            "name": name,
            "mana_cost": "{1}{R}{U}",
            "type_line": "Instant // Instant",
            "oracle_text": "Fire: deal 2 damage // Ice: tap target permanent",
            "power": None, "toughness": None, "loyalty": None, "cmc": 2.0,
        }

    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        _fake_lookup,
    )
    resp = client.get("/api/oracle/Fire // Ice")
    assert resp.status_code == 200
    body = resp.get_json()
    url = body["image_url"]
    # '/' in the name must be %-encoded so the path is unambiguous.
    assert "%2F" in url, f"Expected %-encoded slash in image_url, got: {url!r}"
