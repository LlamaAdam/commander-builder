"""Disk-backed cache for Scryfall card images.

The audit panel and deck dashboard render dozens of card thumbnails per
view. Without a local cache the browser fires a Scryfall round-trip for
every <img> tag, each of which is a 302 redirect to the actual asset.
On a 40-card advisor output that cascade stalled Chrome for 30–60s
during live smoke testing on 2026-05-16.

This module provides a tiny on-disk cache keyed by ``(card_name, size)``
plus a thin fetch helper. The web layer wires a Flask route around it
so the browser only ever talks to the local server for card images;
the server fetches from Scryfall once and serves bytes from disk on
every subsequent request.

Layout::

    <mtg_cards>/images/<size>/<slug>_<sha1-8>.jpg

``<size>`` is one of Scryfall's published image-version strings
(``small`` / ``normal`` / ``large`` / ``png`` / ``art_crop`` /
``border_crop``); the file extension follows: ``.png`` for ``png``,
``.jpg`` otherwise (Scryfall's published encoding per size).

``<slug>`` is the same ``re.sub('[^a-z0-9]+', '_', name.lower())``
slug used by ``scryfall_client._cache_path``; ``<sha1-8>`` is the
first 8 hex chars of the exact card name's SHA-1, appended because
the lossy slug alone collides for distinct names like ``Fire // Ice``
vs. ``Fire Ice`` (see ``cache_path``).

Public surface (all pure-helper, side-effect-localized to disk +
optional injected HTTP fetcher):

  ``cache_path(name, size, root=None)``  → ``pathlib.Path``
  ``content_type_for(size)``             → str
  ``fetch_and_cache(name, size, root=None, http_get=None)`` → bytes
  ``serve_image(name, size, root=None, http_get=None)`` → (bytes, str)
"""
from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional


# Scryfall's published version strings. Anything outside this set
# would be rejected by Scryfall anyway; we validate up front so a
# typo lands as a 400 rather than a wasted round-trip.
ALLOWED_SIZES = frozenset({
    "small", "normal", "large", "png", "art_crop", "border_crop",
})

# Cache quota (default 500 MB). When ``fetch_and_cache`` would push
# the total disk footprint above this, the oldest files (by mtime)
# get evicted until the cache is back under quota. Set via env var
# ``MTG_IMAGE_CACHE_QUOTA_BYTES`` if a deployment needs more headroom.
#
# Rationale: ``normal`` size is ~150 KB, ``large`` ~600 KB, ``png``
# ~1-2 MB. 1000 distinct cards in normal alone is ~150 MB; an
# uncapped cache would grow without bound on a heavy-use machine.
# 500 MB ≈ 3000 unique-card ``normal`` images, plenty for typical
# library sizes (~7k distinct cards across 345 decks per the #018
# smoke run).
_DEFAULT_QUOTA_BYTES = 500 * 1024 * 1024  # 500 MB


def _quota_bytes() -> int:
    """Resolve the cache-quota threshold. Env var override wins so
    operators can tune without code changes."""
    env = os.environ.get("MTG_IMAGE_CACHE_QUOTA_BYTES")
    if env:
        try:
            return int(env)
        except ValueError:
            pass  # Malformed env → fall back to default; no crash.
    return _DEFAULT_QUOTA_BYTES


def _slug(name: str) -> str:
    """Match ``scryfall_client._cache_path`` slug rules so JSON + image
    caches stay aligned for the same card."""
    out = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return out or "unknown"


def _cards_root() -> Path:
    """Resolve the shared mtg_cards directory the same way the
    scryfall_client does. Local import to avoid the
    ``MTG_CARDS_DIR`` env probe firing on module import."""
    from ..scryfall_client import _resolve_cards_dir
    return _resolve_cards_dir()


def content_type_for(size: str) -> str:
    """Scryfall serves ``png`` as PNG and every other size as JPEG.
    Used by the Flask route's response Content-Type header."""
    return "image/png" if size == "png" else "image/jpeg"


def _ext_for(size: str) -> str:
    return ".png" if size == "png" else ".jpg"


def _looks_like_image(data: bytes) -> bool:
    """True if ``data`` starts with a known image magic number.

    Guards the cache against a 200-with-non-image-body: Scryfall (or an
    interposing proxy / rate-limiter) can return HTTP 200 with an HTML or
    JSON error page. Without this check that body gets written as a ``.jpg``
    and served back ``immutable`` for the cache lifetime. We accept JPEG /
    PNG / GIF magics (covers every Scryfall image version) and reject
    everything else (HTML starts with ``<``, JSON with ``{``)."""
    if not data:
        return False
    return (
        data[:3] == b"\xff\xd8\xff"   # JPEG
        or data[:4] == b"\x89PNG"     # PNG (4-byte signature prefix)
        or data[:4] == b"GIF8"        # GIF
    )


def cache_path(name: str, size: str, root: Optional[Path] = None) -> Path:
    """Disk path for the cached ``<name>`` / ``<size>`` image. No IO.

    The filename is ``<slug>_<sha1-8>.<ext>``. The slug alone is NOT
    collision-free: it collapses every non-alnum run to one underscore,
    so distinct cards like ``Fire // Ice`` and a hypothetical
    ``Fire Ice`` both slug to ``fire_ice`` — and whichever was fetched
    first would be served (``immutable``, week-long Cache-Control) for
    the other. Appending the first 8 hex chars of the exact name's
    SHA-1 disambiguates while keeping filenames human-greppable.

    Migration note: pre-hash cache entries (bare ``<slug>.<ext>``) are
    simply never matched again — they miss and the image is refetched
    under the new name; the stale files age out via the LRU quota
    eviction in ``_enforce_quota``. No migration pass needed.
    """
    import hashlib
    digest = hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:8]
    base = root if root is not None else _cards_root()
    return base / "images" / size / (
        f"{_slug(name)}_{digest}{_ext_for(size)}"
    )


def _scryfall_image_url(name: str, size: str) -> str:
    """The Scryfall ``cards/named`` redirect endpoint. Server-side
    HTTP follow lands on the actual CDN asset."""
    qs = urllib.parse.urlencode({"exact": name, "format": "image", "version": size})
    return f"https://api.scryfall.com/cards/named?{qs}"


def _http_get_once(url: str, timeout: float = 20.0) -> bytes:
    """Fetch ``url`` once and return the response body as bytes.

    Plain ``urllib`` so there's no requests/httpx dependency.
    Follows redirects (Scryfall's ``cards/named?format=image`` is a
    302 → CDN). Raises ``urllib.error.HTTPError`` on non-2xx so the
    caller can distinguish 404 (unknown card) from transient failures.
    """
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "commander-builder/0.1 "
                "(+https://github.com/LlamaAdam/commander-builder)"
            ),
            "Accept": "image/png,image/jpeg,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Backoff between retry 1 and the original attempt. Short because
# interactive UI traffic — half a second is the user-perceptible
# upper bound for "the page is loading". One retry only; we don't
# want to amplify a real outage into multi-second per-image stalls.
_RETRY_BACKOFF_SEC = 0.5


def _default_http_get(url: str, timeout: float = 20.0) -> bytes:
    """Fetch ``url`` with one retry on transient failures.

    Catches ``URLError`` (DNS / connection-reset / socket timeout)
    and HTTP 5xx as transient; sleeps ``_RETRY_BACKOFF_SEC`` then
    tries once more. 4xx errors propagate immediately (404 means
    the card legitimately doesn't exist; no point retrying).

    A single transient Scryfall blip on an ``<img>`` tag would
    otherwise surface as a 502 to the browser, which won't
    auto-retry 5xx for image elements. One retry masks the vast
    majority of those transients without amplifying real outages
    into multi-second stalls. (AGENT_BACKLOG #003 / 2026-05-19.)
    """
    import time
    import urllib.error
    try:
        return _http_get_once(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Non-retryable: 4xx including 404. Surface immediately.
        if exc.code is None or exc.code < 500:
            raise
        # 5xx falls through to retry.
    except urllib.error.URLError:
        # Connection reset / DNS / socket timeout — falls through.
        pass
    time.sleep(_RETRY_BACKOFF_SEC)
    return _http_get_once(url, timeout=timeout)


def _images_root(root: Optional[Path] = None) -> Path:
    """Resolve the ``<cards-root>/images`` directory. Used by the
    eviction helper to walk all cached image files regardless of
    which size sub-directory they live in."""
    base = root if root is not None else _cards_root()
    return base / "images"


def _enforce_quota(root: Optional[Path] = None,
                   quota_bytes: Optional[int] = None) -> int:
    """Evict oldest-mtime files until the cache is under quota.

    Returns the count of files deleted. No-op if the cache is
    already under quota. Walks ``<root>/images`` recursively;
    ignores ``.tmp`` partial-write files (those are owned by an
    in-flight ``fetch_and_cache`` and rename atomically when done).

    Eviction is LRU-by-mtime: oldest files go first. Reads don't
    update mtime (we don't ``os.utime`` per-serve because that's a
    syscall per image request — too expensive for interactive
    traffic; mtime stays at last-write which approximates LRU
    well enough for a cache this size).

    Safe to call from a fetch hot-path; bails early if the cache
    isn't over quota.
    """
    images_dir = _images_root(root)
    if not images_dir.exists():
        return 0
    quota = quota_bytes if quota_bytes is not None else _quota_bytes()

    # Stat-and-collect pass. Skip .tmp files (in-flight writes).
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in images_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".tmp":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size

    if total <= quota:
        return 0

    # Sort oldest-first; delete until we're under quota.
    entries.sort(key=lambda t: t[0])
    deleted = 0
    for mtime, size, path in entries:
        if total <= quota:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        deleted += 1
    return deleted


def fetch_and_cache(
    name: str,
    size: str,
    root: Optional[Path] = None,
    http_get: Optional[Callable[[str], bytes]] = None,
    enforce_quota: bool = True,
) -> bytes:
    """Fetch from Scryfall, write to the disk cache, return the bytes.

    Always writes — caller is expected to check the cache first via
    ``serve_image``. Atomic-ish write (tempfile + rename) so a
    half-written file doesn't poison subsequent reads if the process
    is killed mid-fetch.

    ``enforce_quota=True`` (default) runs the LRU eviction pass after
    the write so the disk footprint stays bounded. Set False to
    skip — useful in tests that want to assert on raw write behavior
    without the eviction interfering.
    """
    if size not in ALLOWED_SIZES:
        raise ValueError(f"unsupported size {size!r}; expected one of {sorted(ALLOWED_SIZES)}")
    fetch = http_get or _default_http_get
    data = fetch(_scryfall_image_url(name, size))
    # Never cache a non-image body (HTML/JSON error page returned as 200) —
    # it would be served back as a broken `.jpg` `immutable` until evicted.
    if not _looks_like_image(data):
        raise ValueError(
            f"upstream returned a non-image body for {name!r}/{size} "
            f"({len(data)} bytes); refusing to cache"
        )
    path = cache_path(name, size, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    if enforce_quota:
        _enforce_quota(root=root)
    return data


def serve_image(
    name: str,
    size: str,
    root: Optional[Path] = None,
    http_get: Optional[Callable[[str], bytes]] = None,
) -> tuple[bytes, str]:
    """Return ``(image_bytes, content_type)`` for ``name``/``size``.

    Cache-first: returns the disk bytes if present, otherwise fetches
    and caches before returning. Raises whatever ``http_get`` raises
    on a cache miss the fetch couldn't satisfy — typically
    ``urllib.error.HTTPError`` (404 / 5xx). The caller (Flask route)
    maps those to HTTP responses.
    """
    if size not in ALLOWED_SIZES:
        raise ValueError(f"unsupported size {size!r}; expected one of {sorted(ALLOWED_SIZES)}")
    path = cache_path(name, size, root=root)
    if path.is_file():
        try:
            return path.read_bytes(), content_type_for(size)
        except OSError:
            # TOCTOU: a concurrent fetch's quota eviction can unlink the
            # file between is_file() and read_bytes(). Fall through and
            # refetch rather than surfacing a spurious 502.
            pass
    data = fetch_and_cache(name, size, root=root, http_get=http_get)
    return data, content_type_for(size)
