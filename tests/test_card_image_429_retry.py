"""Bug-fix coverage: /api/card_image/<size>/<name> 429 retry + Retry-After.

Scryfall's image endpoint rate-limits with HTTP 429. The route used to
surface those as opaque 502s to the client; now it retries once with a
short backoff and, if still rate-limited, responds with 429 + a
``Retry-After`` header instead.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

flask = pytest.importorskip("flask")

from commander_builder.web import routes_meta


def _make_429() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.scryfall.com/cards/named",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None,
    )


def _app(tmp_path):
    from commander_builder.web.app import create_app
    return create_app(deck_dir=tmp_path)


def test_card_image_429_retries_once_then_succeeds(tmp_path, monkeypatch):
    """First serve_image raises 429; the route's retry succeeds and the
    response is the cached bytes (not a 429)."""
    calls = {"n": 0}

    def fake_serve(name, size):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_429()
        return (b"\x89PNG\r\n", "image/png")

    # Patch BOTH the binding inside routes_meta (used by the route) and
    # the canonical source in _image_cache, plus skip the real backoff.
    monkeypatch.setattr(routes_meta, "serve_image", fake_serve)
    monkeypatch.setattr(routes_meta, "_CARD_IMAGE_429_RETRY_DELAY_SEC", 0)

    with _app(tmp_path).test_client() as c:
        r = c.get("/api/card_image/normal/Sol%20Ring")
    assert calls["n"] == 2, "expected exactly one retry on 429"
    assert r.status_code == 200
    assert r.data == b"\x89PNG\r\n"


def test_card_image_429_persistent_returns_429_with_retry_after(tmp_path, monkeypatch):
    """Both attempts hit 429 -> client sees 429 + Retry-After (NOT 502)."""
    calls = {"n": 0}

    def fake_serve(name, size):
        calls["n"] += 1
        raise _make_429()

    monkeypatch.setattr(routes_meta, "serve_image", fake_serve)
    monkeypatch.setattr(routes_meta, "_CARD_IMAGE_429_RETRY_DELAY_SEC", 0)

    with _app(tmp_path).test_client() as c:
        r = c.get("/api/card_image/normal/Sol%20Ring")
    assert calls["n"] == 2
    assert r.status_code == 429, "must surface as 429, not 502"
    assert r.headers.get("Retry-After"), "Retry-After header must be present"
    body = r.get_json()
    assert body["error"] == "scryfall rate-limited"


def test_card_image_404_passes_through_unchanged(tmp_path, monkeypatch):
    """404s must NOT be retried -- they're permanent."""
    calls = {"n": 0}

    def fake_serve(name, size):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            url="x", code=404, msg="Not Found", hdrs=None, fp=None,
        )

    monkeypatch.setattr(routes_meta, "serve_image", fake_serve)

    with _app(tmp_path).test_client() as c:
        r = c.get("/api/card_image/normal/Nope")
    assert calls["n"] == 1, "404 must not trigger a retry"
    assert r.status_code == 404
