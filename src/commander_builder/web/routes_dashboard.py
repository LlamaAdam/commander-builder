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
    audit_card_diff,
    get_iteration,
    iteration_graph_for_deck,
    iterations_for_deck,
    pricing_series_for_deck,
    recent_iterations,
    set_milestone,
    update_verdict,
    verdict_breakdown_for_deck,
)
from ._helpers import (
    _bracket_from_filename,
    _build_suggested_adds,
    _iteration_to_dict,
    _resolve_deck_path,
)
from .deck_pricing import printing_savings_for_deck_text


def make_dashboard_blueprint(
    deck_dir: Path,
    knowledge_db: Optional[Path],
    list_decks,
) -> Blueprint:
    """Build a Flask Blueprint for the dashboard + history route group.

    ``list_decks`` is still passed in because it lives in
    ``web/app.py`` (depends on the same deck-dir + user-only flag
    contract). ``_resolve_deck_path`` is imported directly from
    ``_helpers.py`` (was a constructor parameter before the
    2026-05-14 cleanup).
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
        # Enforce the range the error message above already promises —
        # an out-of-range bracket (9, -1) would flow into the power-
        # bracket heuristic and render nonsense tiles.
        if bracket is not None and bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be an integer 1..5"}), 400
        # Default to the [B?] suffix in the filename when the request
        # didn't explicitly pass a bracket — the filename is the user's
        # declared bracket and should beat the heuristic.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id)
        with_advise = request.args.get("advise", "").lower() in (
            "1", "true", "yes",
        )

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
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
        payload = data.to_dict()
        # Cheaper-printing savings (ManaFoundry parity). Computed in
        # deck_pricing (module layering: pricing logic never lives in
        # routes) and attached to the dashboard payload so the pricing
        # tile can render "Save up to $X" without a second request.
        # Failure never blocks the dashboard — same fail-quiet contract
        # as the legality/salt probes in build_dashboard.
        try:
            payload["printing_savings"] = printing_savings_for_deck_text(
                path.read_text(encoding="utf-8"),
            )
        except Exception as exc:  # noqa: BLE001 — dashboard must render regardless
            current_app.logger.warning("printing savings failed: %s", exc)
            payload["printing_savings"] = {
                "total": 0.0, "count": 0, "suggestions": [],
            }
        # Lift picks (ManaFoundry parity — 'Lift Web'). Candidate adds
        # ranked by co-occurrence lift over the harvested (non-[USER]/
        # [CONTROL]) deck corpus in this deck_dir. Computed in
        # lift_analysis (core layer — stats never live in routes) and
        # attached so the UI can render "pairs well with your deck"
        # without a second request. Same fail-quiet contract as
        # printing_savings above: a corpus/scan failure degrades to an
        # empty picks list, never a dashboard 500.
        try:
            from ..lift_analysis import lift_picks_payload
            payload["lift_picks"] = lift_picks_payload(
                path, deck_dir=deck_dir, bracket=bracket,
            )
        except Exception as exc:  # noqa: BLE001 — dashboard must render regardless
            current_app.logger.warning("lift picks failed: %s", exc)
            payload["lift_picks"] = {
                "corpus_size": 0, "band": "overall", "picks": [],
                "reason": "unavailable",
            }
        return jsonify(payload)

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
                # The frontend keys decks by filename stem, but auto-curate
                # writes rows keyed by the Moxfield publicId from the .dck
                # metadata. Query under BOTH ids and merge so iteration
                # history shows up regardless of whether the deck was
                # Moxfield-imported (publicId-keyed rows) or hand-built
                # locally (stem-keyed rows). The two ID schemes never
                # collide so duplicate-row risk is zero. See
                # ``iteration_loop.resolve_deck_id`` for the publicId
                # lookup contract.
                rows = list(iterations_for_deck(deck_id, db_path=knowledge_db))
                public_id: Optional[str] = None
                candidate = (deck_dir / f"{deck_id}.dck")
                if candidate.exists():
                    from ..iteration_loop import resolve_deck_id
                    try:
                        public_id = resolve_deck_id(
                            candidate, fallback=None,
                        )
                    except Exception:
                        public_id = None
                if public_id and public_id != deck_id:
                    extra = list(iterations_for_deck(
                        public_id, db_path=knowledge_db,
                    ))
                    # Merge by id, preserving chronological order
                    # (iterations_for_deck returns oldest-first).
                    seen = {r.id for r in rows}
                    for r in extra:
                        if r.id not in seen:
                            rows.append(r)
                            seen.add(r.id)
                    rows.sort(key=lambda r: r.created_at or "")
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

    @bp.route("/api/iteration_graph")
    def iteration_graph_route():
        """Nodes + edges projection of one deck's iteration chain.

        Returns the shape from ``iteration_graph_for_deck``:

            {
              "deck_id": str,
              "nodes": [{id, iteration_n, bracket, verdict,
                         created_at, card_count, price_usd,
                         audit_version, milestone}, ...],
              "edges": [{from_id, to_id, applied_adds, applied_cuts,
                         rationale, price_delta_usd, bracket_delta}, ...]
            }

        Empty nodes/edges arrays when the deck has no iterations —
        client hides the "View graph" panel rather than crashing
        on null.
        """
        deck_id = request.args.get("deck")
        if not deck_id:
            return jsonify({"error": "deck is required"}), 400
        # Same filename-stem / publicId resolution as /api/iterations:
        # auto-curate writes rows under the Moxfield publicId; the
        # frontend queries by filename stem. Resolve the .dck's
        # publicId and prefer that when it has data, so the verdict
        # UI panel actually surfaces pending rows for Moxfield decks.
        try:
            graph = iteration_graph_for_deck(deck_id, db_path=knowledge_db)
            if not graph.get("nodes"):
                candidate = (deck_dir / f"{deck_id}.dck")
                if candidate.exists():
                    from ..iteration_loop import resolve_deck_id
                    try:
                        public_id = resolve_deck_id(
                            candidate, fallback=None,
                        )
                    except Exception:
                        public_id = None
                    if public_id and public_id != deck_id:
                        graph = iteration_graph_for_deck(
                            public_id, db_path=knowledge_db,
                        )
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "deck_id": deck_id,
            **graph,
        })

    _VALID_VERDICTS = {"kept", "reverted", "neutral", "inconclusive", "pending"}

    @bp.route("/api/iterations/<int:iteration_id>/verdict", methods=["PATCH"])
    def update_iteration_verdict(iteration_id: int):
        """Mark a manual web iteration's verdict (Tier-1.3 fix).

        Before this endpoint existed, the CLI's ``--run-sim`` path was the
        only writer for the verdict column on knowledge_log iteration
        rows. Manual web iterations (audit → propose → apply without a
        Forge sim) landed with ``verdict='pending'`` and stayed pending
        forever, leaving the iteration-graph badges and
        ``/api/verdict_breakdown`` numbers permanently incomplete.

        Body: JSON with ``verdict`` (required, one of
        kept/reverted/neutral/pending) and optional ``notes`` free-text.
        Returns ``{ok: true, iteration_id, verdict}`` on success.

        Errors:
          400  verdict missing or not in the allowed set
          500  sqlite update failed (rare; surfaced for debugging)

        Idempotent — calling with the same verdict twice is a no-op
        at the SQL level (single UPDATE row write). 'pending' is
        accepted explicitly so the UI can clear a verdict that was
        set by mistake.
        """
        body = request.get_json(silent=True) or {}
        verdict = body.get("verdict")
        if not isinstance(verdict, str) or verdict not in _VALID_VERDICTS:
            return jsonify({
                "error": "verdict must be one of kept/reverted/neutral/inconclusive/pending",
            }), 400
        notes = body.get("notes")
        if notes is not None and not isinstance(notes, str):
            return jsonify({"error": "notes must be a string"}), 400
        try:
            update_verdict(
                iteration_id, verdict=verdict, notes=notes,
                db_path=knowledge_db,
            )
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "ok": True,
            "iteration_id": iteration_id,
            "verdict": verdict,
        })

    @bp.route("/api/iterations/<int:iteration_id>/milestone", methods=["PATCH"])
    def update_iteration_milestone(iteration_id: int):
        """Tag (or clear) an iteration with a milestone label
        (AGENT_BACKLOG #012). Mirrors the verdict PATCH endpoint
        shape so the frontend's PATCH handlers can share code.

        Body: JSON with ``milestone`` (string or null/empty to
        clear). Max 64 chars; longer values truncate silently.

        Returns ``{ok: true, iteration_id, milestone}`` on success
        (with ``milestone`` echoing the normalized stored value —
        useful for the UI to display the clipped form when the
        user pasted too much).

        Errors:
          400  milestone wrong type (must be string or null)
          500  sqlite update failed (rare; surfaced for debugging)

        Idempotent. Unknown iteration_id returns 200 silently —
        same fail-quiet contract as ``update_verdict``.
        """
        body = request.get_json(silent=True) or {}
        if "milestone" not in body:
            return jsonify({"error": "milestone field required"}), 400
        label = body.get("milestone")
        if label is not None and not isinstance(label, str):
            return jsonify({
                "error": "milestone must be a string or null",
            }), 400
        try:
            set_milestone(iteration_id, label, db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        # Echo the normalized stored value (truncated, stripped).
        if label is None or not label.strip():
            stored = None
        else:
            stored = label.strip()[:64]
        return jsonify({
            "ok": True,
            "iteration_id": iteration_id,
            "milestone": stored,
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

    @bp.route("/api/audit_diff")
    def audit_diff_route():
        """Card-level delta between two iteration versions (#013).

        ``GET /api/audit_diff?from_id=&to_id=`` -> ``{from, to, diff}`` where
        ``diff`` is added / removed / unchanged cards between the two
        snapshots' [Main] sections (see ``audit_card_diff``). Powers the
        compare-two-versions view in the iteration-history panel.
        """
        def _meta(it):
            return {
                "id": it.id,
                "deck_id": it.deck_id,
                "audit_version": it.audit_version,
                "verdict": it.verdict,
                "milestone": it.milestone,
                "created_at": it.created_at,
            }

        try:
            from_id = int(request.args["from_id"])
            to_id = int(request.args["to_id"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "from_id and to_id are required integers"}), 400

        a = get_iteration(from_id, db_path=knowledge_db)
        b = get_iteration(to_id, db_path=knowledge_db)
        missing = [str(i) for i, it in ((from_id, a), (to_id, b)) if it is None]
        if missing:
            return jsonify({"error": f"iteration(s) not found: {', '.join(missing)}"}), 404

        return jsonify({
            "from": _meta(a),
            "to": _meta(b),
            "diff": audit_card_diff(a.deck_snapshot, b.deck_snapshot),
        })

    return bp
