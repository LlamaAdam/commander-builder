"""FP-007 slice 3 -- Rules / combo lookup endpoints.

Two routes:

``GET /api/rules/combo[?identity=<WUBRG>]``

    Return combos from the ``combo_detection`` module, optionally
    filtered to those whose color identity is a subset of the requested
    identity string.  Each combo is annotated with ``bracket_floor``
    and ``game_ending`` (see ``combo_detection.combo_bracket_floor``).

    Query param ``identity`` is a string of colour symbols, e.g.
    ``"WUB"``, ``"R"``, ``"WUBRG"`` (case-insensitive; order doesn't
    matter).  Omitting it returns ALL combos in the database (the
    fallback + any refreshed cache).

    Response::

        {
          "identity": str | null,   # normalised from query, null if omitted
          "combos": [
            {
              "cards":         [str, ...],
              "produces":      str,
              "popularity":    int | null,
              "bracket_floor": int,
              "game_ending":   bool
            },
            ...
          ],
          "count": int
        }

``GET /api/rules/game_changers``

    Return the WotC Game Changers list as a sorted array.  Served from
    the 7-day cache (``game_changers.load_game_changers``); falls back to
    the bundled list when the fetch fails.

    Response::

        {
          "cards":  [str, ...],   # sorted card names
          "count":  int,
          "source": "cache" | "fallback"
        }

Built via ``make_rules_blueprint()``.  Stateless -- no deck_dir needed.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request


# Recognised WUBRG symbols -- used to strip noise from the identity
# query param (spaces, commas, etc.) before filtering.
_WUBRG = frozenset("WUBRG")


def _normalise_identity(raw: str) -> str:
    """Upper-case and filter to recognised WUBRG symbols only."""
    return "".join(c for c in raw.upper() if c in _WUBRG)


def _combo_matches_identity(combo: dict, identity_set: frozenset) -> bool:
    """True when every colour the combo uses is within ``identity_set``.

    A combo is considered to match a colour identity when all cards in
    the combo *could* be in a deck with that identity.  We use the
    ``identity`` field on the combo when present (Commander Spellbook
    API includes it); otherwise we fall back to ``True`` (don't filter
    combos whose identity is unknown -- the offline fallback list has no
    identity data).
    """
    combo_identity = combo.get("identity")
    if not combo_identity:
        # No identity metadata -- include rather than silently exclude.
        return True
    return frozenset(c.upper() for c in combo_identity if c.upper() in _WUBRG) <= identity_set


def make_rules_blueprint() -> Blueprint:
    """Return a stateless Blueprint for ``/api/rules/*``."""
    bp = Blueprint("rules", __name__)

    @bp.route("/api/rules/combo")
    def combo_lookup():
        """Combos matching an optional colour-identity filter."""
        raw_identity = (request.args.get("identity") or "").strip()
        norm_identity = _normalise_identity(raw_identity) if raw_identity else ""
        identity_set = frozenset(norm_identity) if norm_identity else frozenset()

        from ..combo_detection import (
            load_combos,
            combo_bracket_floor,
            is_game_ending,
        )
        all_combos = load_combos()

        results = []
        for c in all_combos:
            if identity_set and not _combo_matches_identity(c, identity_set):
                continue
            floor = combo_bracket_floor(c)
            results.append({
                "cards": c.get("cards") or [],
                "produces": c.get("produces") or "",
                "popularity": c.get("popularity"),
                "bracket_floor": floor,
                "game_ending": is_game_ending(c),
            })

        return jsonify({
            "identity": norm_identity or None,
            "combos": results,
            "count": len(results),
        })

    @bp.route("/api/rules/game_changers")
    def game_changers_route():
        """WotC Game Changers list (cached 7 days, offline fallback)."""
        from ..game_changers import load_game_changers, CACHE_PATH

        source = "fallback"
        try:
            cards = load_game_changers()
            # Detect whether we served from disk cache or the bundled fallback.
            # ``load_game_changers`` doesn't expose this directly, so we use a
            # heuristic: if the cache file exists and was used, it must have been
            # newer than the TTL.
            if CACHE_PATH.exists():
                source = "cache"
        except Exception:  # noqa: BLE001
            # Fall through -- the caller already got the fallback list from
            # game_changers even on error, but if load_game_changers raised
            # we won't have cards.  Return the empty fallback gracefully.
            cards = set()

        sorted_cards = sorted(cards)
        return jsonify({
            "cards": sorted_cards,
            "count": len(sorted_cards),
            "source": source,
        })

    return bp
