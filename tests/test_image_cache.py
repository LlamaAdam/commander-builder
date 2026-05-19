"""Tests for the disk-backed card-image cache (Tier 2.8 / FP-008).

Background: the audit panel renders one <img> per advisor add. Before
this cache, every <img> fired a Scryfall round-trip + 302 redirect to
the CDN; 40-card outputs stalled Chrome for 30-60s. The cache routes
all browser image requests through a local Flask endpoint that
fetches from Scryfall once per ``(name, size)`` and serves from disk
on every subsequent hit.

Tests cover three layers:

1. ``cache_path`` / ``content_type_for`` — pure path + MIME helpers.
2. ``fetch_and_cache`` / ``serve_image`` — IO logic with an injected
   ``http_get`` callable so no real network traffic flies during tests.
3. ``GET /api/card_image/<size>/<name>`` — Flask integration covering
   happy path, cache hit (no second fetch), 400 on bad size, 404 on
   Scryfall miss, 502 on transient failure.
"""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from commander_builder.web._image_cache import (
    ALLOWED_SIZES,
    cache_path,
    content_type_for,
    fetch_and_cache,
    serve_image,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_cache_path_uses_size_subdir_and_slug(tmp_path):
    """Layout: <root>/images/<size>/<slug>.<ext>. Slug matches the
    scryfall_client convention (lowercase, non-alnum → underscore)."""
    p = cache_path("Sol Ring", "small", root=tmp_path)
    assert p == tmp_path / "images" / "small" / "sol_ring.jpg"


def test_cache_path_png_size_uses_png_extension(tmp_path):
    """Scryfall serves the ``png`` size as actual PNG; every other
    size is JPEG. File extension follows."""
    p = cache_path("Sol Ring", "png", root=tmp_path)
    assert p.suffix == ".png"


def test_cache_path_handles_double_faced_card_names(tmp_path):
    """Double-faced cards use ``//`` separator in Scryfall canonical
    names. The slug collapses non-alnum runs so the filename stays
    safe on Windows."""
    p = cache_path("Bala Ged Recovery // Bala Ged Sanctuary", "normal",
                   root=tmp_path)
    assert p.name == "bala_ged_recovery_bala_ged_sanctuary.jpg"


def test_cache_path_empty_name_falls_back_to_unknown_slug(tmp_path):
    """Defensive: empty / whitespace-only names get a 'unknown' slug
    rather than producing an empty filename."""
    assert cache_path("", "small", root=tmp_path).name == "unknown.jpg"
    assert cache_path("   ", "small", root=tmp_path).name == "unknown.jpg"


def test_content_type_for_png_returns_image_png():
    assert content_type_for("png") == "image/png"


def test_content_type_for_other_sizes_returns_image_jpeg():
    for size in ("small", "normal", "large", "art_crop", "border_crop"):
        assert content_type_for(size) == "image/jpeg"


# ---------------------------------------------------------------------------
# fetch_and_cache + serve_image (with injected http_get)
# ---------------------------------------------------------------------------

def test_fetch_and_cache_writes_bytes_to_disk(tmp_path):
    """fetch_and_cache pulls from the injected http_get, writes the
    bytes to the cache path, and returns the same bytes."""
    payload = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"
    seen_urls = []

    def _fake(url):
        seen_urls.append(url)
        return payload

    data = fetch_and_cache("Sol Ring", "small", root=tmp_path, http_get=_fake)
    assert data == payload
    written = (tmp_path / "images" / "small" / "sol_ring.jpg").read_bytes()
    assert written == payload
    assert len(seen_urls) == 1
    assert "format=image" in seen_urls[0]
    assert "version=small" in seen_urls[0]
    assert "Sol+Ring" in seen_urls[0] or "Sol%20Ring" in seen_urls[0]


def test_fetch_and_cache_rejects_unknown_size(tmp_path):
    """Sizes outside the published Scryfall set raise ValueError up
    front so the route can map to 400 without a wasted round-trip."""
    with pytest.raises(ValueError, match="unsupported size"):
        fetch_and_cache("Sol Ring", "extra_huge", root=tmp_path,
                        http_get=lambda url: b"")


def test_serve_image_cache_hit_does_not_fetch(tmp_path):
    """When the file already exists on disk, serve_image must not
    invoke http_get — that's the entire point of the cache."""
    path = cache_path("Sol Ring", "small", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"on-disk-bytes")

    def _fail(url):
        pytest.fail(f"http_get must not be called on cache hit (url={url})")

    data, content_type = serve_image(
        "Sol Ring", "small", root=tmp_path, http_get=_fail,
    )
    assert data == b"on-disk-bytes"
    assert content_type == "image/jpeg"


def test_serve_image_cache_miss_fetches_then_serves(tmp_path):
    """On cache miss, serve_image populates the cache via http_get
    and returns the freshly-fetched bytes."""
    payload = b"fresh-image-bytes"
    calls = []

    def _fake(url):
        calls.append(url)
        return payload

    data, ctype = serve_image(
        "Sol Ring", "small", root=tmp_path, http_get=_fake,
    )
    assert data == payload
    assert ctype == "image/jpeg"
    # Second call hits cache; http_get must not fire again.
    data2, _ = serve_image(
        "Sol Ring", "small", root=tmp_path,
        http_get=lambda url: pytest.fail("should not fetch twice"),
    )
    assert data2 == payload
    assert len(calls) == 1


def test_fetch_and_cache_atomic_write_no_partial_files(tmp_path):
    """The write goes through ``<path>.tmp`` then atomic rename so a
    half-written file never appears under the final name. Verify the
    tmp file is gone after success."""
    payload = b"complete-bytes"
    fetch_and_cache(
        "Sol Ring", "small", root=tmp_path,
        http_get=lambda url: payload,
    )
    images_dir = tmp_path / "images" / "small"
    tmps = list(images_dir.glob("*.tmp"))
    assert tmps == []
    assert (images_dir / "sol_ring.jpg").read_bytes() == payload


def test_allowed_sizes_matches_scryfall_published_set():
    """Pin the contract — if Scryfall adds a new size we update here
    intentionally, not by accident."""
    assert ALLOWED_SIZES == frozenset({
        "small", "normal", "large", "png", "art_crop", "border_crop",
    })


# ---------------------------------------------------------------------------
# Flask route integration
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_image_cache(tmp_path, monkeypatch):
    """Flask test client with the image cache rooted at tmp_path."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    # Make the image cache resolve to tmp_path so test fetches don't
    # touch the real mtg_cards folder.
    monkeypatch.setattr(
        "commander_builder.web._image_cache._cards_root",
        lambda: tmp_path,
    )
    from commander_builder.web.app import create_app
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def test_card_image_route_serves_bytes_on_happy_path(
    client_with_image_cache, monkeypatch,
):
    """GET /api/card_image/<size>/<name> returns 200 with the bytes
    and the correct Content-Type + Cache-Control headers."""
    client, root = client_with_image_cache
    payload = b"\x89PNG\r\nfake-image"
    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get",
        lambda url, timeout=20.0: payload,
    )
    resp = client.get("/api/card_image/small/Sol Ring")
    assert resp.status_code == 200
    assert resp.data == payload
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert "max-age=604800" in resp.headers["Cache-Control"]
    # Persisted to disk for the next request.
    assert (root / "images" / "small" / "sol_ring.jpg").exists()


def test_card_image_route_uses_cache_on_second_call(
    client_with_image_cache, monkeypatch,
):
    """Second request must not call the network — that's the whole
    point of the cache. We monkeypatch _default_http_get to fail on
    the second invocation."""
    client, _root = client_with_image_cache
    calls = [0]

    def _fake(url, timeout=20.0):
        calls[0] += 1
        if calls[0] > 1:
            pytest.fail("second request must serve from cache")
        return b"cached-bytes"

    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get", _fake,
    )
    r1 = client.get("/api/card_image/small/Sol Ring")
    r2 = client.get("/api/card_image/small/Sol Ring")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.data == r2.data == b"cached-bytes"
    assert calls[0] == 1


def test_card_image_route_rejects_unknown_size(client_with_image_cache):
    """400 (not 404) for bad ``size`` values so the browser can
    distinguish "your URL is wrong" from "Scryfall doesn't know that
    card"."""
    client, _ = client_with_image_cache
    resp = client.get("/api/card_image/jumbo/Sol Ring")
    assert resp.status_code == 400
    body = resp.get_json()
    assert "unsupported size" in body["error"]


def test_card_image_route_returns_404_on_scryfall_miss(
    client_with_image_cache, monkeypatch,
):
    """Scryfall 404 (typo / nonexistent card) propagates as a 404
    from this route so the frontend can render a placeholder."""
    client, _ = client_with_image_cache

    def _http_404(url, timeout=20.0):
        raise urllib.error.HTTPError(
            url, 404, "Not Found", hdrs=None, fp=None,
        )

    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get", _http_404,
    )
    resp = client.get("/api/card_image/small/Made Up Card")
    assert resp.status_code == 404


def test_card_image_route_returns_502_on_transient_failure(
    client_with_image_cache, monkeypatch,
):
    """Network blips / 5xx surface as 502 so the browser doesn't
    cache the failure under the immutable cache header."""
    client, _ = client_with_image_cache

    def _boom(url, timeout=20.0):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get", _boom,
    )
    resp = client.get("/api/card_image/small/Sol Ring")
    assert resp.status_code == 502


def test_card_image_route_handles_double_faced_card_name(
    client_with_image_cache, monkeypatch,
):
    """Double-faced cards have ``//`` in their canonical names. The
    Flask ``<path:name>`` converter must pass the full name through
    intact so the slugger can collapse it correctly."""
    client, _ = client_with_image_cache
    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get",
        lambda url, timeout=20.0: b"mdfc-bytes",
    )
    resp = client.get(
        "/api/card_image/small/Bala Ged Recovery // Bala Ged Sanctuary",
    )
    assert resp.status_code == 200
    assert resp.data == b"mdfc-bytes"
