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
    payload = b"\xff\xd8\xfffresh-image-bytes"  # JPEG magic + body
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
    payload = b"\xff\xd8\xffcomplete-bytes"  # JPEG magic + body
    fetch_and_cache(
        "Sol Ring", "small", root=tmp_path,
        http_get=lambda url: payload,
    )
    images_dir = tmp_path / "images" / "small"
    tmps = list(images_dir.glob("*.tmp"))
    assert tmps == []
    assert (images_dir / "sol_ring.jpg").read_bytes() == payload


def test_fetch_and_cache_rejects_non_image_body(tmp_path):
    """A 200 with an HTML/JSON body (rate-limit / error page) must NOT be
    cached as a .jpg — fetch_and_cache raises and writes nothing, so the next
    request can retry instead of serving garbage immutable for the TTL."""
    html = b"<!DOCTYPE html><html>rate limited</html>"
    with pytest.raises(ValueError, match="non-image"):
        fetch_and_cache("Sol Ring", "small", root=tmp_path,
                        http_get=lambda url: html)
    # Nothing (not even a .tmp) was left behind.
    assert not (tmp_path / "images" / "small" / "sol_ring.jpg").exists()
    assert list((tmp_path / "images").rglob("*")) == [] \
        or all(p.is_dir() for p in (tmp_path / "images").rglob("*"))


def test_looks_like_image_accepts_image_magics_rejects_text():
    from commander_builder.web._image_cache import _looks_like_image
    assert _looks_like_image(b"\xff\xd8\xff...")        # JPEG
    assert _looks_like_image(b"\x89PNG\r\n\x1a\n...")   # PNG
    assert _looks_like_image(b"GIF89a...")              # GIF
    assert not _looks_like_image(b"")                   # empty
    assert not _looks_like_image(b"<html>...")          # HTML error page
    assert not _looks_like_image(b'{"error": "429"}')   # JSON error body


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
        return b"\xff\xd8\xffcached-bytes"  # JPEG magic + body

    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get", _fake,
    )
    r1 = client.get("/api/card_image/small/Sol Ring")
    r2 = client.get("/api/card_image/small/Sol Ring")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.data == r2.data == b"\xff\xd8\xffcached-bytes"
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


# ---------------------------------------------------------------------------
# Retry behavior on transient failures (AGENT_BACKLOG #003)
# ---------------------------------------------------------------------------

def test_default_http_get_retries_once_on_5xx_then_succeeds(monkeypatch):
    """A single 503 followed by a 200 returns the bytes. The retry
    pass-through means an interactive ``<img>`` request masks
    momentary Scryfall flakiness instead of bubbling up as a 502."""
    import urllib.error
    from commander_builder.web import _image_cache
    calls = [0]

    def _fake_once(url, timeout=20.0):
        calls[0] += 1
        if calls[0] == 1:
            raise urllib.error.HTTPError(
                url, 503, "Service Unavailable", hdrs=None, fp=None,
            )
        return b"recovered-bytes"

    monkeypatch.setattr(_image_cache, "_http_get_once", _fake_once)
    monkeypatch.setattr(_image_cache, "_RETRY_BACKOFF_SEC", 0)
    data = _image_cache._default_http_get("https://scryfall.example/x")
    assert data == b"recovered-bytes"
    assert calls[0] == 2


def test_default_http_get_does_not_retry_on_404(monkeypatch):
    """404 means the card legitimately doesn't exist. Retrying would
    waste a round-trip and the second attempt would 404 too. Surface
    the 404 immediately."""
    import urllib.error
    from commander_builder.web import _image_cache
    calls = [0]

    def _fake_once(url, timeout=20.0):
        calls[0] += 1
        raise urllib.error.HTTPError(
            url, 404, "Not Found", hdrs=None, fp=None,
        )

    monkeypatch.setattr(_image_cache, "_http_get_once", _fake_once)
    monkeypatch.setattr(_image_cache, "_RETRY_BACKOFF_SEC", 0)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _image_cache._default_http_get("https://scryfall.example/x")
    assert excinfo.value.code == 404
    assert calls[0] == 1  # NO retry


def test_default_http_get_retries_once_on_url_error_then_succeeds(monkeypatch):
    """URLError (DNS / connection reset / socket timeout) is transient.
    One retry, then propagate if the second attempt also fails."""
    import urllib.error
    from commander_builder.web import _image_cache
    calls = [0]

    def _fake_once(url, timeout=20.0):
        calls[0] += 1
        if calls[0] == 1:
            raise urllib.error.URLError("connection reset")
        return b"second-attempt"

    monkeypatch.setattr(_image_cache, "_http_get_once", _fake_once)
    monkeypatch.setattr(_image_cache, "_RETRY_BACKOFF_SEC", 0)
    assert _image_cache._default_http_get("https://x") == b"second-attempt"
    assert calls[0] == 2


# ---------------------------------------------------------------------------
# Disk-quota eviction (AGENT_BACKLOG #002)
# ---------------------------------------------------------------------------

def test_enforce_quota_no_op_when_under_quota(tmp_path):
    """If the cache total is below quota, no files get deleted —
    eviction is a hot-path call from ``fetch_and_cache`` and must
    be O(stat-and-bail) in the common case."""
    from commander_builder.web._image_cache import _enforce_quota
    images = tmp_path / "images" / "small"
    images.mkdir(parents=True)
    (images / "a.jpg").write_bytes(b"x" * 100)
    (images / "b.jpg").write_bytes(b"y" * 100)
    # Quota 1000 bytes; current 200. No eviction.
    deleted = _enforce_quota(root=tmp_path, quota_bytes=1000)
    assert deleted == 0
    assert (images / "a.jpg").exists()
    assert (images / "b.jpg").exists()


def test_enforce_quota_evicts_oldest_files_first(tmp_path):
    """Over-quota → delete oldest-mtime files until under quota.
    The eviction is LRU-by-mtime: oldest goes first."""
    import os
    import time as _time
    from commander_builder.web._image_cache import _enforce_quota
    images = tmp_path / "images" / "small"
    images.mkdir(parents=True)
    # Three 400-byte files; quota 500 → must delete 2.
    paths = []
    for i, name in enumerate(["old.jpg", "mid.jpg", "new.jpg"]):
        p = images / name
        p.write_bytes(b"x" * 400)
        # Force mtime ordering: old < mid < new.
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
        paths.append(p)
    deleted = _enforce_quota(root=tmp_path, quota_bytes=500)
    assert deleted == 2
    # Newest survives.
    assert (images / "new.jpg").exists()
    assert not (images / "old.jpg").exists()
    assert not (images / "mid.jpg").exists()


def test_enforce_quota_skips_tmp_files(tmp_path):
    """In-flight ``.tmp`` partial writes (from a concurrent
    fetch_and_cache) must not be deleted. They rename atomically
    when done; deleting one mid-write would corrupt the final read."""
    from commander_builder.web._image_cache import _enforce_quota
    images = tmp_path / "images" / "small"
    images.mkdir(parents=True)
    (images / "real.jpg").write_bytes(b"x" * 1000)
    (images / "inflight.jpg.tmp").write_bytes(b"y" * 1000)
    # Over quota; only the real file is eligible.
    deleted = _enforce_quota(root=tmp_path, quota_bytes=500)
    assert deleted == 1
    assert not (images / "real.jpg").exists()
    assert (images / "inflight.jpg.tmp").exists()


def test_enforce_quota_walks_recursively_across_size_subdirs(tmp_path):
    """Cache layout puts files under ``images/<size>/<slug>``;
    eviction must walk all size subdirs, not just one."""
    import os
    from commander_builder.web._image_cache import _enforce_quota
    small = tmp_path / "images" / "small"
    large = tmp_path / "images" / "large"
    small.mkdir(parents=True)
    large.mkdir(parents=True)
    (small / "a.jpg").write_bytes(b"x" * 500)
    (large / "b.jpg").write_bytes(b"y" * 500)
    os.utime(small / "a.jpg", (1_000_000, 1_000_000))   # older
    os.utime(large / "b.jpg", (2_000_000, 2_000_000))   # newer
    # Quota 600 → delete one (the older small one).
    deleted = _enforce_quota(root=tmp_path, quota_bytes=600)
    assert deleted == 1
    assert not (small / "a.jpg").exists()
    assert (large / "b.jpg").exists()


def test_enforce_quota_no_op_when_images_dir_missing(tmp_path):
    """First-time cache use — no ``images/`` directory exists yet.
    Eviction is a clean no-op, not a crash."""
    from commander_builder.web._image_cache import _enforce_quota
    deleted = _enforce_quota(root=tmp_path, quota_bytes=100)
    assert deleted == 0


def test_fetch_and_cache_runs_eviction_after_write(tmp_path):
    """The default fetch path enforces quota so the cache stays
    bounded without any explicit eviction call from the caller."""
    from commander_builder.web._image_cache import fetch_and_cache
    # Pre-fill cache near quota.
    images = tmp_path / "images" / "small"
    images.mkdir(parents=True)
    import os
    (images / "old.jpg").write_bytes(b"x" * 800)
    os.utime(images / "old.jpg", (1_000_000, 1_000_000))
    # Stub the env var to a small quota.
    monkey_env = {"MTG_IMAGE_CACHE_QUOTA_BYTES": "1000"}
    with _MonkeyEnv(monkey_env):
        fetch_and_cache(
            "New Card", "small",
            root=tmp_path,
            http_get=lambda url: b"\xff\xd8\xff" + b"y" * 500,  # JPEG; ~1303 total
        )
    # Old file was evicted (1300 > 1000, oldest first).
    assert not (images / "old.jpg").exists()
    # New file landed.
    assert (images / "new_card.jpg").exists()


def test_fetch_and_cache_enforce_quota_false_skips_eviction(tmp_path):
    """Tests / batch loaders that want raw write behavior pass
    ``enforce_quota=False``."""
    from commander_builder.web._image_cache import fetch_and_cache
    images = tmp_path / "images" / "small"
    images.mkdir(parents=True)
    (images / "old.jpg").write_bytes(b"x" * 800)
    with _MonkeyEnv({"MTG_IMAGE_CACHE_QUOTA_BYTES": "100"}):
        fetch_and_cache(
            "New Card", "small",
            root=tmp_path,
            http_get=lambda url: b"\xff\xd8\xff" + b"y" * 500,
            enforce_quota=False,
        )
    # Both files still present despite tiny quota — eviction skipped.
    assert (images / "old.jpg").exists()
    assert (images / "new_card.jpg").exists()


def test_quota_bytes_env_override(monkeypatch):
    """``MTG_IMAGE_CACHE_QUOTA_BYTES`` env var overrides the
    default constant. Malformed values fall back gracefully."""
    from commander_builder.web import _image_cache
    monkeypatch.setenv("MTG_IMAGE_CACHE_QUOTA_BYTES", "12345")
    assert _image_cache._quota_bytes() == 12345
    monkeypatch.setenv("MTG_IMAGE_CACHE_QUOTA_BYTES", "not-an-int")
    assert _image_cache._quota_bytes() == _image_cache._DEFAULT_QUOTA_BYTES
    monkeypatch.delenv("MTG_IMAGE_CACHE_QUOTA_BYTES")
    assert _image_cache._quota_bytes() == _image_cache._DEFAULT_QUOTA_BYTES


class _MonkeyEnv:
    """Minimal env-var context-manager for the eviction tests that
    can't use the pytest ``monkeypatch`` fixture (no-fixture pytest
    helpers)."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        self._original: dict[str, str | None] = {}

    def __enter__(self):
        import os
        for k, v in self._env.items():
            self._original[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_exc):
        import os
        for k, original in self._original.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


def test_default_http_get_propagates_after_retry_exhausted(monkeypatch):
    """Two consecutive transients surface as the final exception
    (the Flask route then maps to 502). One retry is the cap; we
    don't compound backoffs and stall the user."""
    import urllib.error
    from commander_builder.web import _image_cache

    def _fake_once(url, timeout=20.0):
        raise urllib.error.URLError("persistent fault")

    monkeypatch.setattr(_image_cache, "_http_get_once", _fake_once)
    monkeypatch.setattr(_image_cache, "_RETRY_BACKOFF_SEC", 0)
    with pytest.raises(urllib.error.URLError):
        _image_cache._default_http_get("https://x")


def test_card_image_route_handles_double_faced_card_name(
    client_with_image_cache, monkeypatch,
):
    """Double-faced cards have ``//`` in their canonical names. The
    Flask ``<path:name>`` converter must pass the full name through
    intact so the slugger can collapse it correctly."""
    client, _ = client_with_image_cache
    monkeypatch.setattr(
        "commander_builder.web._image_cache._default_http_get",
        lambda url, timeout=20.0: b"\xff\xd8\xffmdfc-bytes",
    )
    resp = client.get(
        "/api/card_image/small/Bala Ged Recovery // Bala Ged Sanctuary",
    )
    assert resp.status_code == 200
    assert resp.data == b"\xff\xd8\xffmdfc-bytes"
