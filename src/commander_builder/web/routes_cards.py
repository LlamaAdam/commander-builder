"""Card-reference endpoint (FP-007 slice 1).

``GET /api/card/<name>`` returns a richer projection than the lean
``/api/oracle`` tooltip route — enough to render a full card-reference
panel: identity, legality, price, printing, plus the oracle fields. The
unified-app "Cards" search box in the topbar hits this.

Thin layer over ``scryfall_client.lookup_card`` (disk-cached under
``mtg_cards/oracle_snapshots/``); no new datastore. 404 on unknown card,
502 when Scryfall is unreachable and nothing is cached.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

# Scalar fields copied straight from the Scryfall response.
_SCALAR_FIELDS = (
    "name", "mana_cost", "type_line", "oracle_text",
    "power", "toughness", "loyalty", "cmc",
    "set", "set_name", "collector_number", "rarity", "scryfall_uri",
)


def _project_card_reference(card: dict) -> dict:
    """Reduce a Scryfall response to the card-reference panel's fields.

    Missing fields surface as ``None`` / empties so the client renders
    consistently across card types (no power/toughness on a sorcery, no
    price on a digital-only card, etc.)."""
    out: dict = {f: card.get(f) for f in _SCALAR_FIELDS}
    # Color identity as a WUBRG-ordered list (Scryfall already returns a list;
    # default to [] for colorless so the client doesn't branch on None).
    ci = card.get("color_identity")
    out["color_identity"] = list(ci) if isinstance(ci, list) else []
    # Commander legality is the one bracket this app cares about.
    legalities = card.get("legalities")
    out["commander_legal"] = (
        isinstance(legalities, dict)
        and legalities.get("commander") == "legal"
    )
    # USD price as a float (or None) — the raw Scryfall value is a string.
    price = None
    prices = card.get("prices")
    if isinstance(prices, dict) and prices.get("usd"):
        try:
            price = float(prices["usd"])
        except (TypeError, ValueError):
            price = None
    out["price_usd"] = price
    return out


def make_cards_blueprint() -> Blueprint:
    """Build the Blueprint exposing ``/api/card/<name>``. Stateless."""
    bp = Blueprint("cards", __name__)

    @bp.route("/api/card/<path:name>")
    def card_route(name: str):
        # ``path:`` converter so split / DFC names with ``//`` survive URL
        # parsing (e.g. "Fire // Ice").
        name = (name or "").strip()
        if not name:
            return jsonify({"error": "card name required"}), 400

        from ..scryfall_client import lookup_card
        try:
            card = lookup_card(name)
        except Exception as exc:  # noqa: BLE001 — keep other panels alive
            return jsonify({
                "error": "card lookup failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 502
        if card is None:
            return jsonify({"error": "card not found", "name": name}), 404
        return jsonify(_project_card_reference(card))

    return bp
