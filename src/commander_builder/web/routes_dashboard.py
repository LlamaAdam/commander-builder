"""Dashboard + deck-list + iteration-history routes for the web layer.

Five routes live here, all centered on read-only deck data and
historical iteration metadata:

- ``GET /api/decks``               (list .dck files in deck_dir)
- ``GET /api/dashboard``           (full dashboard payload)
- ``GET /api/dashboard/core``      (fast subset — no slow sections)
- ``GET /api/dashboard/section/<name>``  (one slow section, on demand)
- ``GET /api/iterations``          (recent iterations list)
- ``GET /api/pricing_series``      (deck-cost time series)
- ``GET /api/verdict_breakdown``   (per-audit-version kept/reverted)

PROGRESSIVE LOAD (2026-08). A cold premade deck used to sit on a bare
"Loading…" for up to ~90s because ``/api/dashboard`` did every piece of
work serially before its single response: the ``build_dashboard`` core
(Scryfall oracle lookups, usually warm because import populates that
cache), then the printing-savings probe (``lookup_card_prints`` — a
SEPARATE, usually-cold per-card cache, so ~100 network round-trips),
then the lift-picks corpus scan.

The split: ``/api/dashboard/core`` returns everything except those two
slow attachments, and ``/api/dashboard/section/<name>`` computes exactly
one of them. The UI paints core immediately and fills the slow tiles
from skeletons.

``/api/dashboard`` is UNCHANGED and still returns the union — the
payload contract other consumers (tests, the bracket-override refetch,
any external script) rely on keeps working. All three routes share the
``_pricing_section`` / ``_lift_section`` helpers, so the two shapes can
never drift.

SIM COVERAGE (2026-08, roadmap #4). Both dashboard payloads additionally
carry an additive ``sim_coverage`` key — cards the vendored Forge build
has no script for (see ``_sim_coverage``). Forge sims silently omit
such cards ("An unsupported card was requested" in the logs, which
``log_parser`` counts but nothing ever showed a user), so the dashboard
now flags the gap before anyone trusts a sim verdict built on them.

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

# --- Deferred (slow) dashboard sections --------------------------------
#
# Each entry maps a section name to a builder that returns
# ``(payload_dict, ok)``. ``payload_dict`` uses the SAME keys the full
# ``/api/dashboard`` payload uses, so the client can splice a section
# response straight into its dashboard model. ``ok=False`` means the
# section is unavailable (network/corpus failure) — the client renders
# the established inline "unavailable" state for that tile only and the
# rest of the page is untouched.
#
# The fallback dicts are byte-identical to what ``/api/dashboard`` has
# always emitted on failure, so an unavailable section degrades exactly
# the way the monolithic route already did.


def _pricing_section(path: Path, deck_dir: Path, bracket: Optional[int]):
    """Cheaper-printing savings (ManaFoundry parity).

    The expensive one: ``lookup_card_prints`` has its own per-card disk
    cache which is cold for a freshly imported deck, so this is ~1
    network round-trip per distinct card.
    """
    try:
        return {
            "printing_savings": printing_savings_for_deck_text(
                path.read_text(encoding="utf-8"),
            ),
        }, True
    except Exception as exc:  # noqa: BLE001 — dashboard must render regardless
        current_app.logger.warning("printing savings failed: %s", exc)
        return {
            "printing_savings": {"total": 0.0, "count": 0, "suggestions": []},
        }, False


def _lift_section(path: Path, deck_dir: Path, bracket: Optional[int]):
    """Lift picks — co-occurrence scan over the harvested deck corpus.

    Cost scales with the number of .dck files in ``deck_dir`` (hundreds
    on a real install), so it is deferred alongside pricing even though
    it never touches the network.
    """
    try:
        from ..lift_analysis import lift_picks_payload
        return {
            "lift_picks": lift_picks_payload(
                path, deck_dir=deck_dir, bracket=bracket,
            ),
        }, True
    except Exception as exc:  # noqa: BLE001 — dashboard must render regardless
        current_app.logger.warning("lift picks failed: %s", exc)
        return {
            "lift_picks": {
                "corpus_size": 0, "band": "overall", "picks": [],
                "reason": "unavailable",
            },
        }, False


_DEFERRED_SECTIONS = {
    "pricing": _pricing_section,
    "lift_picks": _lift_section,
}


# Empty/unavailable sim-coverage shape. ``available: False`` means "the
# vendored Forge corpus could not be consulted" — a fresh checkout with
# no vendor/forge, or a read error — which is NOT the same statement as
# "every card is supported"; the UI hides the tile instead of showing a
# confident zero (the fail-quiet convention every other probe follows).
_SIM_COVERAGE_UNAVAILABLE = {
    "available": False,
    "checked_count": 0,
    "unsupported_count": 0,
    "unsupported_names": [],
}


def _sim_coverage(path: Path) -> dict:
    """Forge sim coverage for one deck: which cards the vendored Forge
    build has NO card script for.

    WHY (2026-08, roadmap #4): ``log_parser`` has always extracted
    Forge's "An unsupported card was requested" lines, but that parse
    result is folded away inside ``compare_versions`` and never reaches
    a web response — so a deck full of cards Forge can't simulate runs
    its A/B sims as silently-partial data (Forge just plays on without
    those cards). This probe surfaces the same DB gap *before* any sim,
    by checking every [Commander]/[Main] card name against the vendored
    Forge card-script corpus (``forge_cards_loader`` — the exact corpus
    whose misses produce the log line at sim time).

    Purely local + offline (zip/directory lookups, no network, no JVM),
    fast enough to ride inline on the core dashboard payload. Additive:
    a new ``sim_coverage`` key, nothing renamed or removed.

    Returns ``{available, checked_count, unsupported_count,
    unsupported_names}``; the unavailable shape (``available: False``)
    on any failure — never a 500, never a fabricated all-clear.
    """
    try:
        from .. import dck_utils
        from ..forge_cards_loader import CardsLoader
        from ..forge_runner import VENDOR_FORGE

        text = path.read_text(encoding="utf-8")
        names: list[str] = []
        seen: set[str] = set()
        for section in ("Commander", "Main"):
            for name in dck_utils.section_card_names(text, section):
                key = name.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    names.append(name.strip())
        if not names:
            return dict(_SIM_COVERAGE_UNAVAILABLE)
        with CardsLoader.locate(VENDOR_FORGE) as loader:
            unsupported = sorted(
                n for n in names if loader.load_one(n) is None
            )
        return {
            "available": True,
            "checked_count": len(names),
            "unsupported_count": len(unsupported),
            "unsupported_names": unsupported,
        }
    except FileNotFoundError:
        # No vendor/forge (fresh checkout, CI) — expected, not an error.
        return dict(_SIM_COVERAGE_UNAVAILABLE)
    except Exception as exc:  # noqa: BLE001 — dashboard must render regardless
        current_app.logger.warning("sim coverage probe failed: %s", exc)
        return dict(_SIM_COVERAGE_UNAVAILABLE)


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

    def _resolve_request():
        """Shared arg parsing for the three dashboard routes.

        Returns ``(path, bracket, error_response)`` — exactly one of
        ``path`` / ``error_response`` is None. Keeping this in one place
        is what lets ``/api/dashboard``, ``/api/dashboard/core`` and
        ``/api/dashboard/section/<name>`` agree on which deck (and which
        declared bracket) a given query string names; a client that
        splices a section response into a core payload MUST have both
        computed against the same deck.
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return None, None, (
                jsonify({"error": "bracket must be an integer 1..5"}), 400
            )
        # Enforce the range the error message above already promises —
        # an out-of-range bracket (9, -1) would flow into the power-
        # bracket heuristic and render nonsense tiles.
        if bracket is not None and bracket not in (1, 2, 3, 4, 5):
            return None, None, (
                jsonify({"error": "bracket must be an integer 1..5"}), 400
            )
        # Default to the [B?] suffix in the filename when the request
        # didn't explicitly pass a bracket — the filename is the user's
        # declared bracket and should beat the heuristic.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id)

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return None, None, (
                jsonify({
                    "error": "deck not found",
                    "deck": deck_id,
                    "path": explicit,
                }), 404
            )
        return path, bracket, None

    def _core_payload(path: Path, bracket: Optional[int]) -> dict:
        """The fast dashboard body: everything ``build_dashboard``
        produces, with no deferred section attached."""
        with_advise = request.args.get("advise", "").lower() in (
            "1", "true", "yes",
        )
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
        # Forge sim coverage (roadmap #4): additive key on BOTH the full
        # and the core payloads — fast, offline, fail-quiet, so it needs
        # no deferred-section round trip.
        payload["sim_coverage"] = _sim_coverage(path)
        return payload

    @bp.route("/api/dashboard")
    def dashboard():
        """Full payload — core PLUS every deferred section inlined.

        Unchanged contract: kept for consumers that want one blocking
        request (the test suite, scripts, and any client that predates
        the progressive split). The web UI uses ``/api/dashboard/core``
        + per-section fetches instead.
        """
        path, bracket, err = _resolve_request()
        if err is not None:
            return err
        payload = _core_payload(path, bracket)
        # Cheaper-printing savings + lift picks (ManaFoundry parity).
        # Both are computed in the core layer (deck_pricing /
        # lift_analysis — pricing and stats logic never live in routes)
        # and attached fail-quiet: a network or corpus failure degrades
        # to the empty shape, never a dashboard 500. Same contract as
        # the legality/salt probes inside build_dashboard.
        for builder in (_pricing_section, _lift_section):
            section, _ok = builder(path, deck_dir, bracket)
            payload.update(section)
        return jsonify(payload)

    @bp.route("/api/dashboard/core")
    def dashboard_core():
        """Fast dashboard subset — the progressive-load first paint.

        Same keys as ``/api/dashboard`` MINUS ``printing_savings`` and
        ``lift_picks``, plus a ``deferred_sections`` list naming the
        sections the client must fetch separately. Clients that see an
        unknown name in that list can simply skip it (forward-compatible
        by construction), which is why the names are advertised rather
        than hardcoded on both sides.
        """
        path, bracket, err = _resolve_request()
        if err is not None:
            return err
        payload = _core_payload(path, bracket)
        payload["deferred_sections"] = sorted(_DEFERRED_SECTIONS)
        return jsonify(payload)

    @bp.route("/api/dashboard/section/<name>")
    def dashboard_section(name: str):
        """One deferred dashboard section, computed on demand.

        Returns ``{section, status, data, reason}`` where ``status`` is
        ``"ok"`` or ``"unavailable"`` and ``data`` carries the same keys
        the full payload uses (``printing_savings`` / ``lift_picks``),
        so the client splices it in without a per-section translation
        table. ``unavailable`` still ships the empty fallback shape in
        ``data`` so a client that ignores ``status`` renders an empty
        tile rather than crashing on undefined.

        404 for an unknown section name or an unresolvable deck. A
        failing section is deliberately NOT a 5xx: it is an expected,
        per-tile outage, and the page it belongs to has already
        painted.
        """
        builder = _DEFERRED_SECTIONS.get(name)
        if builder is None:
            return jsonify({
                "error": f"unknown dashboard section: {name!r}",
                "sections": sorted(_DEFERRED_SECTIONS),
            }), 404
        path, bracket, err = _resolve_request()
        if err is not None:
            return err
        data, ok = builder(path, deck_dir, bracket)
        return jsonify({
            "section": name,
            "status": "ok" if ok else "unavailable",
            "data": data,
            "reason": None if ok else "section computation failed",
        })

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
