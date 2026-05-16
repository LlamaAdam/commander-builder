"""Oracle-text presentation endpoint for the web layer.

Single route — ``GET /api/oracle/<card_name>`` — returning the
subset of Scryfall card metadata needed to render an MTG-card-style
tooltip + side panel in the audit UI:

    {
      "name":        str,
      "mana_cost":   str | None,    # e.g. "{2}{R}{R}"
      "type_line":   str | None,    # e.g. "Legendary Creature — Goblin Warrior"
      "oracle_text": str | None,    # full multi-paragraph rules text
      "power":       str | None,    # creatures only (Scryfall ships as str)
      "toughness":   str | None,    # creatures only
      "loyalty":     str | None,    # planeswalkers only
      "cmc":         float | None,  # mana value
    }

Built via ``make_oracle_blueprint()``. The route is the UI surface
for FP-009 (Oracle-text card-reference store presentation helper):
the substrate already exists in ``scryfall_client.lookup_card``
which caches per-card JSON under ``mtg_cards/oracle_snapshots/``,
so this blueprint is a thin projection layer over that store.

Caching strategy
----------------
Two layers:

1. ``scryfall_client.lookup_card`` already disk-caches each card
   indefinitely under ``oracle_snapshots/<slug>.json`` — Scryfall's
   oracle text is errata-stable for hours-to-weeks, so we treat the
   disk cache as authoritative until explicitly refreshed (the
   existing ``refresh_card`` helper handles that elsewhere).
2. In-process LRU cache wrapping the projected payload, so repeat
   hits within a single Flask process avoid re-deserializing the
   JSON snapshot. TTL'd at 1 hour to bound the chance of serving a
   stale oracle after an errata cycle without forcing a full
   process restart.

The route ALWAYS returns the projected shape on cache hit; we do
NOT proxy the full Scryfall response (which can be 5-10 KB per
card and carries fields the UI doesn't use).

404 vs 502
----------
- 404: card name doesn't match any Scryfall entry (typo, custom
  card, etc.).
- 502: Scryfall is unreachable AND we have no cached copy. The UI
  should treat this as "tooltip unavailable, keep the click-to-
  zoom thumbnail working" — don't kill the whole audit panel.
"""

from __future__ import annotations

import time
from threading import Lock

from flask import Blueprint, jsonify


# In-process projection cache. Key = lowercased card name, value =
# ``(expires_at_epoch, projection_dict)``. Bounded at ~512 entries to
# keep memory in check — that's enough for any realistic audit
# session (the largest deck is 100 cards; the average-deck preview
# adds ~75 more; salt-list adds another 100; so 512 covers ~5×
# turnover before we evict).
_PROJECTION_CACHE: dict[str, tuple[float, dict]] = {}
_PROJECTION_CACHE_LOCK = Lock()
_PROJECTION_CACHE_TTL_SEC = 3600.0  # 1 hour — see module docstring
_PROJECTION_CACHE_MAX = 512


# Fields we project out of the Scryfall response. The UI needs
# enough to render a card-frame-like tooltip + a full oracle panel,
# but no more — we deliberately drop Scryfall fields like
# ``image_uris`` (thumbnails go through ``cardImageUrl`` directly
# on the client) and ``prices`` (already surfaced elsewhere in the
# audit payload).
_PROJECTED_FIELDS = (
    "name",
    "mana_cost",
    "type_line",
    "oracle_text",
    "power",
    "toughness",
    "loyalty",
    "cmc",
)


def _project_card(card: dict) -> dict:
    """Reduce a Scryfall response to the UI's tooltip/side-panel
    fields. Missing fields surface as ``None`` so the client can
    decide how to render (e.g. no power/toughness on a sorcery)."""
    return {field: card.get(field) for field in _PROJECTED_FIELDS}


def _cache_get(key: str) -> dict | None:
    now = time.time()
    with _PROJECTION_CACHE_LOCK:
        entry = _PROJECTION_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < now:
            # Expired — drop and treat as miss.
            _PROJECTION_CACHE.pop(key, None)
            return None
        return payload


def _cache_put(key: str, payload: dict) -> None:
    with _PROJECTION_CACHE_LOCK:
        # Eviction: simplest-thing-that-could-possibly-work — when
        # the cache is full, drop the oldest entry by expiration. The
        # TTL means every entry has the same lifespan, so insertion
        # order ≈ expiration order; we just pop an arbitrary item.
        if len(_PROJECTION_CACHE) >= _PROJECTION_CACHE_MAX:
            try:
                _PROJECTION_CACHE.pop(next(iter(_PROJECTION_CACHE)))
            except StopIteration:
                pass
        _PROJECTION_CACHE[key] = (time.time() + _PROJECTION_CACHE_TTL_SEC, payload)


def _clear_projection_cache() -> None:
    """Test-only: drop the in-process cache so a fresh test fixture
    sees a clean slate. The route logic doesn't otherwise need to
    expose this — process restart clears the cache too."""
    with _PROJECTION_CACHE_LOCK:
        _PROJECTION_CACHE.clear()


def make_oracle_blueprint() -> Blueprint:
    """Build a Flask Blueprint exposing ``/api/oracle/<card_name>``.

    Stateless — no deck-dir / knowledge-db dependency, so the
    factory takes no args. Registered alongside the other audit-
    panel-supporting blueprints in ``web/app.py``.
    """
    bp = Blueprint("oracle", __name__)

    @bp.route("/api/oracle/<path:card_name>")
    def oracle_route(card_name: str):
        """Return projected Scryfall metadata for ``card_name``.

        Uses ``path:`` converter (not the default ``string:``) so
        names with embedded slashes (split cards, "Fire // Ice", "Who
        // What // When // Where // Why") survive the URL parsing
        without 404ing on Flask's segment splitter.
        """
        # Defensive normalization — strip whitespace, fold case for
        # the cache key. We DON'T fold case for the upstream Scryfall
        # call: ``lookup_card`` uses the exact-named endpoint which is
        # itself case-insensitive, but preserving the user's casing
        # in error messages helps debugging.
        name = (card_name or "").strip()
        if not name:
            return jsonify({"error": "card name required"}), 400
        cache_key = name.lower()

        cached = _cache_get(cache_key)
        if cached is not None:
            # Surface the cache-hit fact for the tests + for browser
            # devtools sanity-checking. The response body is identical
            # to a miss otherwise.
            resp = jsonify(cached)
            resp.headers["X-Oracle-Cache"] = "hit"
            return resp

        # Lazy import so the blueprint module stays cheap to load
        # (and tests can monkey-patch ``lookup_card`` on the
        # ``scryfall_client`` module without dragging it in at
        # import time).
        from ..scryfall_client import lookup_card
        try:
            card = lookup_card(name)
        except Exception as exc:  # noqa: BLE001
            # Network blip / Scryfall outage / unexpected JSON. Keep
            # the UI's other panels alive — 502 says "the upstream
            # data store failed, don't retry me immediately."
            return jsonify({
                "error": "oracle lookup failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 502

        if card is None:
            return jsonify({
                "error": "card not found",
                "name": name,
            }), 404

        payload = _project_card(card)
        _cache_put(cache_key, payload)
        resp = jsonify(payload)
        resp.headers["X-Oracle-Cache"] = "miss"
        return resp

    return bp
