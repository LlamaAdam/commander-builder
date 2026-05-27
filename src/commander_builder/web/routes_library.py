"""FP-007 slice 2 -- cross-deck library search.

``GET /api/library?card=<name>``

Returns the sorted list of deck IDs that run the named card across the
``.dck`` set in ``deck_dir``.  Backed by the already-landed helper
``web._helpers.decks_containing_card``.

Response shape::

    {
      "card":  str,           # normalised card name from the query
      "decks": [str, ...],    # sorted deck IDs (filename stems)
      "count": int            # len(decks) -- convenience for the UI
    }

400 when the ``card`` param is missing or blank; never 404 (an empty
``decks`` list means "no decks run this card", which is valid data).

Built via ``make_library_blueprint(deck_dir)``.  The deck_dir is the
same one the rest of the app uses -- no parallel datastore.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request


def make_library_blueprint(deck_dir: Path) -> Blueprint:
    """Return a Blueprint exposing ``/api/library``."""
    bp = Blueprint("library", __name__)

    @bp.route("/api/library")
    def library_search():
        """Which of my decks run a given card?

        Query param ``card`` is matched case-insensitively against every
        ``[Commander]`` and ``[Main]`` line in the ``.dck`` set (edition
        tails stripped so ``1 Sol Ring|CLB|871`` matches ``sol ring``).
        """
        card = (request.args.get("card") or "").strip()
        if not card:
            return jsonify({"error": "card param is required"}), 400

        from ._helpers import decks_containing_card
        decks = decks_containing_card(deck_dir, card)
        return jsonify({"card": card, "decks": decks, "count": len(decks)})

    return bp
