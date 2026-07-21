"""Audit + advise routes for the commander-builder web layer.

Three routes live here, all variations on "run the improvement
advisor and project its output for the UI":

- ``GET /api/audit`` — synchronous full-report endpoint. Returns
  the proposed deck text + the added/removed payload in one
  response. Blocks for ~6-8s on the Claude path.
- ``GET /api/audit/stream`` — SSE variant emitting phase events
  (diagnosis → manabase → primary → complete) so the UI can
  render progressive results. Same query params + final payload
  shape as the sync endpoint.
- ``GET /api/advise`` — lightweight variant returning just the
  ``suggested_adds`` projection. Used by the dashboard panel
  when it wants advice without the full proposed-deck assembly.

The blueprint is built via ``make_audit_blueprint(deck_dir)`` so
it closes over the deck directory the app was started with.
Tests can mock ``commander_builder.improvement_advisor.advise``
or ``_advise_steps`` to drive the routes offline.

Extracted from ``web/app.py`` as part of the 2026-05-13 blueprint
refactor (tier-3 issue #3.1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, stream_with_context

# BYO-key handling (2026-07-19 rework): the per-request Anthropic key is
# threaded EXPLICITLY through ``advise(..., api_key=...)`` down to the
# Anthropic client constructor. The old approach staged the key in the
# process-global ``os.environ`` (with a lock + restore-in-finally), which
# (a) serialized all Claude requests behind one env window, and (b) was
# one missed code path away from concurrent requests reading each other's
# keys or wiping one mid-API-call. The web layer must NEVER write
# per-request secrets into ``os.environ``.

from ..edhrec_client import fetch_salt_list
from ._helpers import (
    _apply_swaps_to_dck,
    _bracket_from_filename,
    _build_suggested_adds,
    _match_pct_from_evidence,
    _pad_main_to_99,
    _resolve_deck_path,
    _total_price_for_deck_text,
    project_average_deck_preview,
    project_salt_warning,
    read_protected_cards,
)


# Human-readable backend names for warning text — underscore IDs
# read awkwardly in user-facing prose ("bracket_peers backend fell
# back" → "Bracket-peers backend fell back").
_SOURCE_LABEL = {
    "heuristic": "EDHREC heuristic",
    "claude": "Claude analyst",
    "bracket_peers": "Bracket-peers",
}


def _resolve_byo_key(header_value: str) -> str:
    """Resolve the effective BYO Anthropic key for an audit request.

    Single source of truth, in precedence order (FP-011 unification):
      1. ``X-Anthropic-API-Key`` request header — an ephemeral per-request
         override (rarely used now the Settings panel exists).
      2. ``config.json``'s ``anthropic_api_key`` — what the Settings panel
         writes; the durable per-user key.
    Returns ``""`` when neither is set, in which case the caller passes
    ``api_key=None`` to the advisor so a deployment-level
    ``ANTHROPIC_API_KEY`` (credentials file / container secret) still
    applies. The resolved key is only ever threaded as an explicit
    parameter — never written into ``os.environ`` (see module docstring
    note on the 2026-07-19 BYO-key rework).
    """
    header_value = (header_value or "").strip()
    if header_value:
        # Validate the header against the same key-shape regex the
        # Settings PUT path enforces. A garbage value would otherwise be
        # staged into os.environ and surface (possibly echoed back in an
        # SDK error message) instead of failing cleanly to the fallback.
        try:
            from .. import config_store
            if config_store._ANTHROPIC_KEY_RE.match(header_value):
                return header_value
        except Exception:  # noqa: BLE001 — config must never break an audit
            pass
        return ""
    try:
        from .. import config_store
        return (config_store.load_config().get("anthropic_api_key") or "").strip()
    except Exception:  # noqa: BLE001 — config must never break an audit
        return ""


# Empty shape for deck_health when computation fails entirely. The UI
# renders tiles based on key presence; missing keys would crash the
# renderer, so we always ship the full structure even on failure.
_EMPTY_DECK_HEALTH = {
    "mdfc": {"count": 0, "cards": []},
    "spell_density": {
        "non_permanent_count": 0,
        "total_main_count": 0,
        "ratio": None,
    },
    "mana_sinks": {"count": 0, "cards": []},
    "wincon_protection": {"count": 0, "cards": []},
    "self_mill": {"count": 0, "cards": []},
    "role_targets": {"roles": {}, "under_built": []},
}


def _count_main_lines(deck_text: str) -> int:
    """Sum quantity prefixes across the [Main] section.

    Counts what's ACTUALLY in the deck text, not derived from
    recommendation list lengths. The pre-fix headline math was
    ``kept + len(added_payload) + padded_count`` which counted
    every recommendation including the ones that balancing /
    protection / bracket-cap dropped -- producing a misleading
    main_count (e.g. 143 on a deck whose proposed_text was 99
    cards). This helper walks proposed_text once and sums the
    qty prefixes the way Forge actually parses them.

    Thin wrapper over ``dck_utils.count_main_cards``.
    """
    from ..dck_utils import count_main_cards
    return count_main_cards(deck_text)


def _compute_deck_health_safe(deck_text: str) -> dict:
    """Wrap ``deck_health.compute_deck_health`` so a Scryfall outage
    or unexpected parse failure doesn't take down the whole audit
    response. Returns the empty-shape dict on any exception."""
    try:
        from ..deck_health import compute_deck_health
        return compute_deck_health(deck_text)
    except Exception:  # noqa: BLE001 -- defensive at the route layer
        return dict(_EMPTY_DECK_HEALTH)


_EMPTY_COMBO_ASSESSMENT = {
    "combos": [], "recommended_bracket": 1, "violations": [],
    "within_bracket": True,
}


def _assess_combos_safe(deck_text: str, bracket: int) -> dict:
    """Wrap ``combo_detection.assess_deck_brackets`` so a combo-DB read or
    parse failure can't take down the audit. Surfaces detected infinite/win
    combos and whether they push the deck above its declared bracket."""
    try:
        from ..combo_detection import assess_deck_brackets
        return assess_deck_brackets(deck_text, bracket)
    except Exception:  # noqa: BLE001 -- defensive at the route layer
        return dict(_EMPTY_COMBO_ASSESSMENT)


def _fallback_warning(
    requested: str,
    actual_source: str,
    fallback_reason,
    byo_key: str,
) -> "str | None":
    """The fall-back warning shared by both audit endpoints.

    advise() silently degrades to the EDHREC heuristic when the
    requested backend can't run (no Claude key, no bracket peers, a
    network blip). Three branches, in priority order, tell the user
    exactly why so they can fix it or accept the degraded source:
      1. a concrete fallback_reason from advise()
      2. the specific "no API key" guidance (claude requested, no key
         anywhere) — this branch was MISSING on the SSE path before
         the two endpoints were unified here
      3. a generic "requested but unavailable" message
    Returns None when the effective source matches what was requested.
    """
    if actual_source == requested:
        return None
    requested_label = _SOURCE_LABEL.get(requested, requested)
    if fallback_reason:
        return (
            f"{requested_label} fell back to EDHREC heuristic. "
            f"Reason: {fallback_reason}"
        )
    if (requested == "claude"
            and not byo_key
            and not os.environ.get("ANTHROPIC_API_KEY")):
        return (
            "Claude analyst was requested but no API key was provided. "
            "Open Settings and set your Anthropic API key (sk-ant-…), "
            "then try again."
        )
    return (
        f"{requested_label} was requested but unavailable — "
        "falling back to EDHREC heuristic."
    )


def _build_audit_payload(
    report,
    *,
    original: str,
    deck_id,
    bracket: int,
    requested: str,
    byo_key: str,
) -> dict:
    """Assemble the full audit response shared by the sync ``/api/audit``
    endpoint and the SSE ``/api/audit/stream`` ``complete`` event.

    Single source of truth for proposed-text assembly, pricing, salt
    annotations, and the fall-back warning. The two endpoints used to
    carry near-identical copies of this (~160 lines each) that had
    drifted: the stream path omitted the "no API key" warning branch and
    mis-named the diagnosis field ``rationale`` (the UI reads
    ``diagnosis`` on both paths, so the streamed diagnosis never
    rendered). Both bugs are fixed by routing both endpoints through here.
    """
    proposed_text, added, removed, kept = _apply_swaps_to_dck(
        original, report.recommendations,
    )
    # Pad sub-100 source decks with basics mirroring the deck's color
    # distribution so Forge will load the proposed deck; ``kept`` stays
    # truthful and ``padded_count`` is surfaced separately.
    post_swap_main = kept + len(added)
    proposed_text, padded_count, padded_breakdown = _pad_main_to_99(
        proposed_text, post_swap_main,
    )
    original_total, original_priced = _total_price_for_deck_text(original)
    proposed_total, proposed_priced = _total_price_for_deck_text(proposed_text)
    if original_total is not None and proposed_total is not None:
        price_delta = round(proposed_total - original_total, 2)
    else:
        price_delta = None
    # EDHREC salt list once per audit (cached 7 days). Best-effort:
    # empty dict on fetch failure → no salt annotations, no warning.
    try:
        salt_map = fetch_salt_list()
    except Exception:  # noqa: BLE001
        salt_map = {}
    # Surface ALL recommendations, not just those that landed in
    # proposed_text after adds==cuts balancing; ``applied`` flags which
    # are in the Forge-ready text vs loose suggestions. (Live-audit
    # 2026-05-14: the cut-gate emits zero cuts on sparse EDHREC data,
    # which previously dropped every add silently.)
    applied_add_set = {n.lower() for n in added}
    applied_cut_set = {n.lower() for n in removed}
    added_payload = [
        {
            "card": rec.card,
            "rationale": rec.reason or "",
            "match_pct": _match_pct_from_evidence(rec.evidence),
            "price_usd": (rec.evidence or {}).get("price_usd"),
            "name_known": getattr(rec, "name_known", True),
            "source": (rec.evidence or {}).get("source"),
            "applied": rec.card.lower() in applied_add_set,
            "salt": salt_map.get(rec.card.lower()),
        }
        for rec in report.recommendations
        if rec.action == "add"
    ]
    removed_payload = [
        {
            "card": rec.card,
            "rationale": rec.reason or "",
            "name_known": getattr(rec, "name_known", True),
            "applied": rec.card.lower() in applied_cut_set,
            "salt": salt_map.get(rec.card.lower()),
        }
        for rec in report.recommendations
        if rec.action == "cut"
    ]
    unknown_card_count = sum(
        1 for entry in added_payload + removed_payload
        if entry["name_known"] is False
    )
    actual_source = getattr(report, "source", "heuristic")
    fallback_reason = getattr(report, "fallback_reason", None)
    warning = _fallback_warning(
        requested, actual_source, fallback_reason, byo_key,
    )
    return {
        "deck": deck_id,
        "bracket": bracket,
        "proposed_text": proposed_text,
        "added": added_payload,
        "removed": removed_payload,
        "kept_count": kept,
        # Count quantity-prefixed [Main] lines directly so the headline
        # matches what Forge will load (not derived from rec-list sizes).
        "main_count": _count_main_lines(proposed_text),
        "diagnosis": getattr(report.diagnosis, "pattern_summary", "") or "",
        "weakness_signals": list(
            getattr(report.diagnosis, "weakness_signals", []) or []
        ),
        "source": actual_source,
        # Older clients read ``requested_llm``; newer read
        # ``requested_source``. Both populated for transition.
        "requested_llm": requested,
        "requested_source": requested,
        "warning": warning,
        "basics_padded": padded_count,
        "basics_padded_breakdown": padded_breakdown,
        "unknown_card_count": unknown_card_count,
        "skipped_for_saturation": list(
            getattr(report, "skipped_for_saturation", []) or []
        ),
        "bracket_peer_ref_count": int(
            getattr(report, "bracket_peer_ref_count", 0) or 0,
        ),
        "original_price_usd": original_total,
        "proposed_price_usd": proposed_total,
        "price_delta_usd": price_delta,
        "n_priced_cards_original": original_priced,
        "n_priced_cards_proposed": proposed_priced,
        "average_deck_preview": project_average_deck_preview(
            getattr(report, "average_deck", None),
            getattr(report, "edhrec_categories", {}) or {},
            original,
        ),
        "salt_warning": project_salt_warning(original, salt_map, bracket),
        "protected_cards": read_protected_cards(original),
        "deck_health": _compute_deck_health_safe(original),
        "combo_assessment": _assess_combos_safe(original, bracket),
    }


def make_audit_blueprint(deck_dir: Path) -> Blueprint:
    """Build a Flask Blueprint for the audit/advise route group.

    Closes over ``deck_dir`` so route handlers don't need to query
    Flask's app context for it. ``_resolve_deck_path`` is imported
    from ``_helpers.py`` directly (no longer threaded through the
    constructor — moved there in the 2026-05-14 cleanup).
    """
    bp = Blueprint("audit", __name__)

    @bp.route("/api/audit")
    def audit_route():
        """Run the improvement advisor and return a **full proposed
        deck** (the user's current deck with the recommended swaps
        applied), not just the list of changes.

        This matches the intent of prompts/moxfield_audit_v3.md:
        produce the ideal version of the deck, then surface the diff.
        The web client takes the `proposed_text` field and drops it
        into the Edit modal so the user can preview / tweak before
        running the A/B sim.

        Returns::

            {
              "deck": "<id>", "bracket": int,
              "proposed_text": "<full .dck blob>",
              "added": [{"card", "rationale", "match_pct", "price"}, ...],
              "removed": [{"card", "rationale"}, ...],
              "kept_count": int,
              "main_count": int,
            }
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return jsonify({"error": "bracket must be int"}), 400
        # Range check for parity with the other bracket-accepting
        # routes — an out-of-range bracket (9, -1) would steer the
        # advisor's bracket-targeted suggestions into nonsense.
        if bracket is not None and bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be 1..5"}), 400
        # Filename bracket is the default when not overridden.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id) or 3

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404

        # Backend selection. Two query params, listed in priority order:
        #   ?source=heuristic|claude|bracket_peers  (preferred)
        #   ?llm=heuristic|claude                   (legacy alias)
        # ``source`` is the newer, more expressive parameter; if both
        # are passed, source wins (it can name the bracket_peers backend
        # that llm can't). Defaults to heuristic — no token cost,
        # always available.
        source = (request.args.get("source") or "").strip().lower()
        llm = (request.args.get("llm") or "heuristic").strip().lower()
        if source:
            if source not in ("heuristic", "claude", "bracket_peers"):
                return jsonify({
                    "error": "source must be 'heuristic', 'claude', or 'bracket_peers'",
                }), 400
            requested = source
        else:
            if llm not in ("heuristic", "claude"):
                return jsonify({
                    "error": "llm must be 'heuristic' or 'claude'",
                }), 400
            requested = llm
        use_claude = requested == "claude"
        byo_key = _resolve_byo_key(request.headers.get("X-Anthropic-API-Key", ""))
        # Budget mode skips ABU duals + fetches from the manabase
        # essentials safety net. Truthy query values: 1, true, yes.
        budget_raw = (request.args.get("budget") or "").strip().lower()
        budget = budget_raw in ("1", "true", "yes")
        # Optional model override. Accepts any string the SDK accepts;
        # defaults to whatever DEFAULT_CLAUDE_MODEL is set to in
        # improvement_advisor (Sonnet today). Most-cost-effective
        # value: "claude-haiku-4-5" (~3-5x cheaper than Sonnet).
        claude_model = (request.args.get("model") or "").strip() or None

        try:
            from ..improvement_advisor import advise as _advise, DEFAULT_CLAUDE_MODEL
            # BYO key rides the explicit api_key parameter (None → the
            # advisor uses the deployment-level env/credentials key).
            # Concurrent requests each carry their own key on their own
            # stack — no process-global state, no cross-request races.
            report = _advise(
                path, bracket=bracket,
                source=requested,
                use_claude=use_claude,
                claude_model=claude_model or DEFAULT_CLAUDE_MODEL,
                budget=budget,
                api_key=(byo_key or None) if use_claude else None,
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            return jsonify({
                "error": "audit failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 503

        original = path.read_text(encoding="utf-8")
        return jsonify(_build_audit_payload(
            report,
            original=original,
            deck_id=deck_id,
            bracket=bracket,
            requested=requested,
            byo_key=byo_key,
        ))

    @bp.route("/api/audit/stream")
    def audit_stream_route():
        """Server-Sent Events variant of ``/api/audit``.

        Streams audit progress as four event types so the UI can
        render partial results while the slow source-specific
        recommender (Claude can take 6-8s) is still running:

        - ``event: diagnosis`` (~10ms) — commanders + DeckDiagnosis
          so the UI shows the weakness/category panel immediately.
        - ``event: manabase`` (~50ms) — curated manabase essentials
          (shocks / fetches / bond / tribal lands) for the deck's
          color identity.
        - ``event: primary`` (100ms-8s) — the source-specific recs
          (heuristic / bracket_peers / claude) with effective source
          + fallback_reason populated.
        - ``event: complete`` — the final assembled payload (same
          shape as ``/api/audit``) ready to drop into the existing
          UI render code.
        - ``event: error`` — emitted on input-validation failure or
          unrecoverable mid-stream exception. Carries
          ``{error, detail}``.

        Query params and headers mirror ``/api/audit`` so the client
        can switch endpoints by URL alone. Default Cache-Control: no
        — SSE responses should never be cached.
        """
        # --- Parse query params (same shape as the sync endpoint) ----
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return jsonify({"error": "bracket must be int"}), 400
        # Range check for parity with the other bracket-accepting routes.
        if bracket is not None and bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be 1..5"}), 400
        if bracket is None:
            bracket = _bracket_from_filename(deck_id) or 3

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404

        source = (request.args.get("source") or "").strip().lower()
        llm = (request.args.get("llm") or "heuristic").strip().lower()
        if source:
            if source not in ("heuristic", "claude", "bracket_peers"):
                return jsonify({
                    "error": (
                        "source must be 'heuristic', 'claude', "
                        "or 'bracket_peers'"
                    ),
                }), 400
            requested = source
        else:
            if llm not in ("heuristic", "claude"):
                return jsonify({
                    "error": "llm must be 'heuristic' or 'claude'",
                }), 400
            requested = llm
        use_claude = requested == "claude"
        byo_key = _resolve_byo_key(request.headers.get("X-Anthropic-API-Key", ""))
        budget_raw = (request.args.get("budget") or "").strip().lower()
        budget = budget_raw in ("1", "true", "yes")
        claude_model = (request.args.get("model") or "").strip() or None

        # --- Stream generator -----------------------------------------
        def event_stream():
            """Drive ``_advise_steps()`` and emit one SSE block per
            phase. The complete-phase block carries the same payload
            shape the sync ``/api/audit`` endpoint returns so the
            client can reuse its render code with no further branching.
            """
            from ..improvement_advisor import (
                _advise_steps, DEFAULT_CLAUDE_MODEL,
            )

            def _sse(event_name: str, payload: dict) -> str:
                # SSE wire format: "event: <name>\ndata: <json>\n\n".
                # Splitting data on newlines is the spec — we always
                # emit one ``data:`` line (single-line JSON) so the
                # client's EventSource can parse it cleanly.
                return (
                    f"event: {event_name}\n"
                    f"data: {json.dumps(payload)}\n\n"
                )

            # BYO key rides the explicit api_key parameter for the whole
            # generator lifetime — it lives on this generator's stack, not
            # in os.environ. The old approach held an env mutation (plus a
            # global lock) across the entire SSE stream, which both
            # serialized all Claude requests and widened the window in
            # which other threads could observe the wrong key.
            try:
                for phase in _advise_steps(
                    path, bracket=bracket,
                    source=requested,
                    use_claude=use_claude,
                    claude_model=claude_model or DEFAULT_CLAUDE_MODEL,
                    budget=budget,
                    api_key=(byo_key or None) if use_claude else None,
                ):
                    if phase.phase == "error":
                        yield _sse("error", {
                            "error": phase.data.get("reason", "unknown"),
                            "detail": phase.data.get("type", "RuntimeError"),
                            "where": phase.data.get("where"),
                        })
                        return
                    if phase.phase == "complete":
                        # Drop-in payload identical to the sync endpoint —
                        # both paths go through _build_audit_payload so the
                        # warning logic + field names never diverge again.
                        report = phase.data["report"]
                        original = path.read_text(encoding="utf-8")
                        yield _sse("complete", _build_audit_payload(
                            report,
                            original=original,
                            deck_id=deck_id,
                            bracket=bracket,
                            requested=requested,
                            byo_key=byo_key,
                        ))
                        continue
                    # Intermediate phases (diagnosis / manabase / primary)
                    # — emit the phase's data dict as-is. The client
                    # branches on the event name. Keep these payloads
                    # JSON-serializable (they already are because
                    # _advise_steps asdict'd the dataclasses).
                    yield _sse(phase.phase, phase.data)
            except Exception as exc:  # noqa: BLE001
                # Last-ditch — generator raised after work began. The
                # client treats this the same as the input-validation
                # error phase (close the stream, show the message).
                yield _sse("error", {
                    "error": "audit failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                })

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                # Disable proxy buffering so events flush to the client
                # as soon as ``yield`` runs. Without this, nginx /
                # Apache will batch the response and the streaming
                # benefit disappears.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.route("/api/advise")
    def advise_route():
        """Standalone advise endpoint — same shape as /api/dashboard's
        suggested_adds but doesn't require a full dashboard rebuild."""
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return jsonify({"error": "bracket must be an integer 1..5"}), 400
        # Enforce the range the error message above already promises.
        if bracket is not None and bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be an integer 1..5"}), 400
        if bracket is None:
            bracket = _bracket_from_filename(deck_id) or 3

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({
                "error": "deck not found",
                "deck": deck_id,
                "path": explicit,
            }), 404

        try:
            suggested = _build_suggested_adds(path, bracket)
        except Exception as exc:
            return jsonify({
                "error": "advise unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 503

        return jsonify({
            "deck": deck_id, "bracket": bracket,
            "suggestions": suggested,
        })

    return bp
