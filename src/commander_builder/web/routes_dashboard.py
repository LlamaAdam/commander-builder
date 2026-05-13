"""Dashboard + deck-list + iteration-history routes for the web layer.

Five routes live here, all centered on read-only deck data and
historical iteration metadata:

- ``GET /api/decks``               (list .dck files in deck_dir)
- ``GET /api/dashboard``           (full dashboard payload)
- ``GET /api/iterations``          (recent iterations list)
- ``GET /api/pricing_series``      (deck-cost time series)
- ``GET /api/verdict_breakdown``   (per-audit-version kept/reverted)

Built via ``make_dashboard_blueprint(deck_dir, knowledge_db,
list_decks, resolve_deck_path)``. The two helper functions are
passed in (rather than imported) because they're still defined in
``web/app.py`` at module scope and we want to avoid circular
imports.

Extracted from ``web/app.py`` as part of the 2026-05-13 blueprint
refactor (tier-3 issue #3.1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from ..deck_dashboard import build_dashboard
from ..knowledge_log import (
    iterations_for_deck,
    pricing_series_for_deck,
    recent_iterations,
    verdict_breakdown_for_deck,
)
from ._helpers import (
    _bracket_from_filename,
    _build_suggested_adds,
    _iteration_to_dict,
)


def make_dashboard_blueprint(
    deck_dir: Path,
    knowledge_db: Optional[Path],
    list_decks,
    resolve_deck_path,
) -> Blueprint:
    """Build a Flask Blueprint for the dashboard + history route group.

    ``list_decks`` and ``resolve_deck_path`` are the corresponding
    helpers still living in ``web/app.py``. Passing them as
    arguments keeps the blueprint stateless and dodges circular
    imports as more route groups get extracted.
    """
    bp = Blueprint("dashboard", __name__)

    @bp.route("/api/decks")
    def decks():
        # Default: only [USER] decks. Pass ?all=1 to include filler/pool.
        all_flag = request.args.get("all", "").lower() in ("1", "true", "yes")
        return jsonify({
            "decks": list_decks(deck_dir, user_only=not all_flag),
        })

    @bp.route("/api/dashboard")
    def dashboard():
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return jsonify({"error": "bracket must be an integer 1..5"}), 400
        # Default to the [B?] suffix in the filename when the request
        # didn't explicitly pass a bracket — the filename is the user's
        # declared bracket and should beat the heuristic.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id)
        with_advise = request.args.get("advise", "").lower() in (
            "1", "true", "yes",
        )

        path = resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({
                "error": "deck not found",
                "deck": deck_id,
                "path": explicit,
            }), 404

        suggested = None
        if with_advise:
            try:
                suggested = _build_suggested_adds(path, bracket or 3)
            except Exception as exc:
                # advise() can fail for many reasons (missing EDHREC,
                # missing commander, network); the dashboard still
                # renders without suggestions.
                suggested = None
                current_app.logger.warning("advise failed: %s", exc)

        data = build_dashboard(path, bracket=bracket, suggested=suggested)
        return jsonify(data.to_dict())

    @bp.route("/api/iterations")
    def iterations():
        deck_id = request.args.get("deck")
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        limit = max(1, min(limit, 500))

        try:
            if deck_id:
                rows = iterations_for_deck(deck_id, db_path=knowledge_db)
            else:
                rows = recent_iterations(limit=limit, db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500

        return jsonify({
            "iterations": [_iteration_to_dict(r) for r in rows],
            "deck_id": deck_id,
            "count": len(rows),
        })

    @bp.route("/api/pricing_series")
    def pricing_series_route():
        """Time-series of total deck cost across one deck's iteration
        chain. Powers the dashboard sparkline that surfaces cost
        evolution over time.

        Returns ``{deck_id, count, points: [{iteration_id, captured_at,
        total_price_usd}, ...]}``. Empty points list when the deck
        has no iterations OR none of them captured a pricing snapshot.
        """
        deck_id = request.args.get("deck")
        if not deck_id:
            return jsonify({"error": "deck is required"}), 400
        try:
            points = pricing_series_for_deck(
                deck_id, db_path=knowledge_db,
            )
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "deck_id": deck_id,
            "count": len(points),
            "points": points,
        })

    @bp.route("/api/verdict_breakdown")
    def verdict_breakdown_route():
        """Per-audit-version verdict counts for one deck.

        Returns ``{deck_id, total_iterations, breakdown: {<version>:
        {kept, reverted, neutral, pending, total}}}``. UI consumes this
        to show "kept 4/5 v3 swaps, kept 2/3 v4 swaps" when the deck
        has accumulated enough iterations to be meaningful (≥5).
        """
        deck_id = request.args.get("deck")
        if not deck_id:
            return jsonify({"error": "deck is required"}), 400
        try:
            breakdown = verdict_breakdown_for_deck(
                deck_id, db_path=knowledge_db,
            )
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        total = sum(b.get("total", 0) for b in breakdown.values())
        return jsonify({
            "deck_id": deck_id,
            "total_iterations": total,
            "breakdown": breakdown,
        })

    return bp
