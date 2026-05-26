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
    Returns ``""`` when neither is set, in which case the caller leaves
    ``os.environ`` untouched so a deployment-level ``ANTHROPIC_API_KEY``
    (credentials file / container secret) still applies.
    """
    header_value = (header_value or "").strip()
    if header_value:
        return header_value
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
    """
    import re as _re
    line_pat = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    total = 0
    in_main = False
    for raw in deck_text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            in_main = s.lower() == "[main]"
            continue
        if not in_main:
            continue
        m = line_pat.match(s)
        if m:
            try:
                total += int(m.group(1))
            except (TypeError, ValueError):
                total += 1
    return total


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
            saved_key = os.environ.get("ANTHROPIC_API_KEY")
            if use_claude and byo_key:
                os.environ["ANTHROPIC_API_KEY"] = byo_key
            try:
                report = _advise(
                    path, bracket=bracket,
                    source=requested,
                    use_claude=use_claude,
                    claude_model=claude_model or DEFAULT_CLAUDE_MODEL,
                    budget=budget,
                )
            finally:
                # Always restore env, even on raise — the BYO key must
                # not linger in the process for unrelated requests.
                if use_claude and byo_key:
                    if saved_key is None:
                        os.environ.pop("ANTHROPIC_API_KEY", None)
                    else:
                        os.environ["ANTHROPIC_API_KEY"] = saved_key
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            return jsonify({
                "error": "audit failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 503

        original = path.read_text(encoding="utf-8")
        proposed_text, added, removed, kept = _apply_swaps_to_dck(
            original, report.recommendations,
        )
        # Backlog item: pad sub-100 source decks. The advisor balances
        # adds==cuts so any source-deck deficit (e.g. the Goblin deck's
        # 71 mainboard) is preserved into the proposed deck and Forge
        # refuses to load it. Top up with basics mirroring the deck's
        # existing color distribution. `kept` stays truthful (cards
        # from the source that survived cuts); `padded_count` is
        # surfaced separately so the UI can show what we synthesized.
        post_swap_main = kept + len(added)
        proposed_text, padded_count, padded_breakdown = _pad_main_to_99(
            proposed_text, post_swap_main,
        )
        # Compute the original + proposed deck total prices so the UI
        # can show "$420 → $537 (+$117)" alongside the diff. Tier-2
        # backlog item — feeds the cost-evolution chart's per-swap
        # delta and lets budget-mode users see the cost impact of
        # their audit at a glance. Best-effort: ``_total_price_for_
        # deck_text`` returns ``(None, 0)`` when no priced cards are
        # available (Scryfall down, all-digital-only deck), which
        # the UI translates to "—" instead of "$0.00."
        original_total, original_priced = _total_price_for_deck_text(original)
        proposed_total, proposed_priced = _total_price_for_deck_text(proposed_text)
        if original_total is not None and proposed_total is not None:
            price_delta = round(proposed_total - original_total, 2)
        else:
            price_delta = None
        # Surface ALL adds/cuts the advisor produced, not just those
        # that landed in proposed_text after _apply_swaps_to_dck's
        # adds==cuts balancing. The ``applied`` flag tells the UI
        # which entries are in proposed_text (safe to drop into
        # Forge) vs which are loose recommendations (the user
        # needs to choose what to swap them in for).
        #
        # Live-audit 2026-05-14 surfaced the need: the new heuristic
        # cut-gate (MIN_EDHREC_SIGNAL_FOR_CUTS) correctly emits zero
        # cuts when EDHREC's data is sparse, but the previous
        # ``applied_add_set`` filter dropped EVERY add silently
        # because min(adds, 0) = 0. Users got "no recommendations"
        # instead of "here are 8 manabase upgrades, choose what to
        # cut yourself."
        # Pull EDHREC's salt list once per audit (cached 7 days)
        # so we can flag socially-spicy picks. Best-effort: empty
        # dict on fetch failure → no salt annotations, no warning.
        try:
            salt_map = fetch_salt_list()
        except Exception:  # noqa: BLE001
            salt_map = {}
        applied_add_set = {n.lower() for n in added}
        applied_cut_set = {n.lower() for n in removed}
        added_payload = [
            {
                "card": rec.card,
                "rationale": rec.reason or "",
                "match_pct": _match_pct_from_evidence(rec.evidence),
                "price_usd": (rec.evidence or {}).get("price_usd"),
                # name_known: True/False/None. Default True for legacy
                # stubs/recs that predate the validator.
                "name_known": getattr(rec, "name_known", True),
                # Per-rec source so the UI can render a distinguishing
                # badge when match_pct is null (manabase essentials,
                # vanilla Claude recs). Values mirror evidence.source.
                "source": (rec.evidence or {}).get("source"),
                # True when this rec landed in proposed_text;
                # False = "recommended but no cut to balance against."
                # UI styles unapplied entries with a muted look + a
                # "needs manual cut" pill so the user knows the deck
                # text isn't pre-built for them.
                "applied": rec.card.lower() in applied_add_set,
                # EDHREC salt score (0..5) — None when the card
                # isn't in the top-100 salty list. UI shows a "salt"
                # pill when ≥ 2.0; low-bracket users can audit-wise
                # avoid table-talk-problematic picks.
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
                # Cutting a salty card is GOOD news at low bracket;
                # surface the score so the UI can highlight it.
                "salt": salt_map.get(rec.card.lower()),
            }
            for rec in report.recommendations
            if rec.action == "cut"
        ]
        # Hallucination flag: only count cards Scryfall *confirmed* are
        # fake (False). Skip None (validator couldn't reach Scryfall)
        # so a network blip never spuriously raises the count.
        unknown_card_count = sum(
            1 for entry in added_payload + removed_payload
            if entry["name_known"] is False
        )
        actual_source = getattr(report, "source", "heuristic")
        fallback_reason = getattr(report, "fallback_reason", None)
        warning = None
        requested_label = _SOURCE_LABEL.get(requested, requested)
        if actual_source != requested:
            # advise() silently falls back to heuristic when the
            # requested backend can't run (no Claude API key, no
            # bracket-peer references found, network blip). Tell the
            # UI exactly why so the user can fix it or accept the
            # degraded source instead of guessing.
            if fallback_reason:
                warning = (
                    f"{requested_label} fell back to EDHREC heuristic. "
                    f"Reason: {fallback_reason}"
                )
            elif (requested == "claude"
                  and not byo_key
                  and not os.environ.get("ANTHROPIC_API_KEY")):
                warning = (
                    "Claude analyst was requested but no API key was "
                    "provided. Open Settings and set your Anthropic API "
                    "key (sk-ant-…), then try again."
                )
            else:
                warning = (
                    f"{requested_label} was requested but unavailable — "
                    "falling back to EDHREC heuristic."
                )
        return jsonify({
            "deck": deck_id,
            "bracket": bracket,
            "proposed_text": proposed_text,
            "added": added_payload,
            "removed": removed_payload,
            "kept_count": kept,
            # main_count reflects what's ACTUALLY in proposed_text -- the
            # cards Forge will load. Previously this was computed as
            # ``kept + len(added_payload) + padded_count`` which counts
            # every recommendation including ones balancing/protection
            # dropped, producing a misleading headline (e.g. First Sliver
            # B3 reported 143 main on a 99-card proposed deck). Count
            # quantity-prefixed [Main] lines directly to keep the
            # headline truthful.
            "main_count": _count_main_lines(proposed_text),
            "diagnosis": getattr(report.diagnosis, "pattern_summary", ""),
            "weakness_signals": list(getattr(
                report.diagnosis, "weakness_signals", [],
            ) or []),
            "source": actual_source,
            # The originally-requested backend. Older clients read
            # `requested_llm`; new clients can read `requested_source`.
            # Both populated to the same value for transition.
            "requested_llm": requested,
            "requested_source": requested,
            "warning": warning,
            # Backlog: padding info so the UI can warn if we synthesized
            # basic lands to bring a sub-100 source deck up to legal size.
            "basics_padded": padded_count,
            "basics_padded_breakdown": padded_breakdown,
            # Hallucination defense — non-zero on Claude analyst path
            # when the LLM invented a card name Scryfall doesn't recognize.
            "unknown_card_count": unknown_card_count,
            # Adds the saturation guard dropped because the deck already
            # has enough cards in that role bucket. Each entry:
            # {card, role, deck_count, threshold}. Lets the UI show
            # "skipped 3 ramp adds — you already have 13" so a short
            # list isn't mistaken for the advisor giving up.
            "skipped_for_saturation": list(
                getattr(report, "skipped_for_saturation", []) or []
            ),
            # Non-zero only when source=claude AND the LLM call shipped
            # bracket-peer references in the prompt. Lets the UI render
            # 'Claude analyst (5 peer refs)' so users can tell when
            # archetype data informed the recommendations vs. just
            # EDHREC averages.
            "bracket_peer_ref_count": int(
                getattr(report, "bracket_peer_ref_count", 0) or 0,
            ),
            # Proposed-deck pricing — feeds the cost-evolution view
            # + lets the UI surface "$X → $Y (Δ)" alongside the diff.
            # All three fields are nullable: None when no priced
            # cards were found in the corresponding deck text.
            "original_price_usd": original_total,
            "proposed_price_usd": proposed_total,
            "price_delta_usd": price_delta,
            "n_priced_cards_original": original_priced,
            "n_priced_cards_proposed": proposed_priced,
            # EDHREC bracket-specific sample build — feeds the
            # audit panel's collapsible "Average deck preview"
            # section. None when EDHREC has no published average
            # deck for this commander+bracket or the fetch failed,
            # so the UI knows to hide the <details> entirely.
            "average_deck_preview": project_average_deck_preview(
                getattr(report, "average_deck", None),
                getattr(report, "edhrec_categories", {}) or {},
                original,
            ),
            # Aggregate salt-score banner — fires above the audit
            # recommendations when the user's current deck carries
            # salty picks at a low bracket. Null at B4/B5 (high-
            # power tables expect salt) or when EDHREC's salt-list
            # was unreachable. The per-rec ``salt`` annotations on
            # added/removed entries are unaffected.
            "salt_warning": project_salt_warning(
                original, salt_map, bracket,
            ),
            # Per-deck protected-cards list from [metadata] Protect=
            # entries. Surfaced so the UI can badge protected cards
            # in the cuts list with a 🔒 — the auto-curate path
            # already strips these, but the audit endpoint produces
            # advisory suggestions and the user might still want to
            # see what the advisor flagged.
            "protected_cards": read_protected_cards(original),
            # Deck-health tile row signals: MDFC count, spell density,
            # mana sinks, wincon-specific protection, self-mill
            # enablement. These surface deck-construction quality
            # signals the advisor's recommendation engine doesn't
            # directly act on but the user benefits from seeing
            # (e.g. "this combo deck has 0 Silence-class protection").
            # Best-effort: any individual signal that fails returns
            # its empty shape so the rest of the panel renders.
            "deck_health": _compute_deck_health_safe(original),
            # Infinite/win combos detected in the deck + whether they push
            # it above its declared bracket (WotC: two-card infinite combos
            # are restricted below B4). `violations` is the actionable set.
            "combo_assessment": _assess_combos_safe(original, bracket),
        })

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

            saved_key = os.environ.get("ANTHROPIC_API_KEY")
            if use_claude and byo_key:
                os.environ["ANTHROPIC_API_KEY"] = byo_key
            try:
                for phase in _advise_steps(
                    path, bracket=bracket,
                    source=requested,
                    use_claude=use_claude,
                    claude_model=claude_model or DEFAULT_CLAUDE_MODEL,
                    budget=budget,
                ):
                    if phase.phase == "error":
                        yield _sse("error", {
                            "error": phase.data.get("reason", "unknown"),
                            "detail": phase.data.get("type", "RuntimeError"),
                            "where": phase.data.get("where"),
                        })
                        return
                    if phase.phase == "complete":
                        # Re-do the same post-assembly the sync endpoint
                        # does so the client gets a drop-in payload.
                        report = phase.data["report"]
                        original = path.read_text(encoding="utf-8")
                        proposed_text, added, removed, kept = (
                            _apply_swaps_to_dck(
                                original, report.recommendations,
                            )
                        )
                        post_swap_main = kept + len(added)
                        proposed_text, padded_count, padded_breakdown = (
                            _pad_main_to_99(proposed_text, post_swap_main)
                        )
                        # Compute original + proposed deck prices for
                        # the cost-delta UI. Same logic as the sync
                        # endpoint — see its matching block for the
                        # rationale.
                        (
                            original_total, original_priced,
                        ) = _total_price_for_deck_text(original)
                        (
                            proposed_total, proposed_priced,
                        ) = _total_price_for_deck_text(proposed_text)
                        if (original_total is not None
                                and proposed_total is not None):
                            price_delta = round(
                                proposed_total - original_total, 2,
                            )
                        else:
                            price_delta = None
                        # Pull EDHREC's salt list once per audit
                        # (cached 7 days). Best-effort: empty dict on
                        # fetch failure → no salt annotations.
                        try:
                            salt_map = fetch_salt_list()
                        except Exception:  # noqa: BLE001
                            salt_map = {}
                        # Surface ALL recommendations (not just those
                        # that landed in proposed_text). See the sync
                        # endpoint's matching block for the rationale —
                        # the live-audit 2026-05-14 cut-gate fix would
                        # otherwise silently drop every add when cuts=0.
                        applied_add_set = {n.lower() for n in added}
                        applied_cut_set = {n.lower() for n in removed}
                        added_payload = [
                            {
                                "card": rec.card,
                                "rationale": rec.reason or "",
                                "match_pct": _match_pct_from_evidence(
                                    rec.evidence,
                                ),
                                "price_usd": (rec.evidence or {}).get(
                                    "price_usd",
                                ),
                                "name_known": getattr(
                                    rec, "name_known", True,
                                ),
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
                                "name_known": getattr(
                                    rec, "name_known", True,
                                ),
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
                        fallback_reason = getattr(
                            report, "fallback_reason", None,
                        )
                        warning = None
                        if actual_source != requested and fallback_reason:
                            warning = (
                                f"{_SOURCE_LABEL.get(requested, requested)} "
                                f"fell back to EDHREC heuristic. "
                                f"Reason: {fallback_reason}"
                            )
                        yield _sse("complete", {
                            "deck": deck_id,
                            "bracket": bracket,
                            "proposed_text": proposed_text,
                            "added": added_payload,
                            "removed": removed_payload,
                            "kept_count": kept,
                            # Count from proposed_text directly so the
                            # headline matches what Forge will load --
                            # see the sync /api/audit endpoint for the
                            # rationale and the pre-fix bug context.
                            "main_count": _count_main_lines(proposed_text),
                            "rationale": (
                                getattr(report.diagnosis, "pattern_summary", "")
                                or ""
                            ),
                            "weakness_signals": list(getattr(
                                report.diagnosis, "weakness_signals", [],
                            ) or []),
                            "source": actual_source,
                            "requested_llm": requested,
                            "requested_source": requested,
                            "warning": warning,
                            "basics_padded": padded_count,
                            "basics_padded_breakdown": padded_breakdown,
                            "unknown_card_count": unknown_card_count,
                            "skipped_for_saturation": list(
                                getattr(
                                    report, "skipped_for_saturation", [],
                                ) or []
                            ),
                            "bracket_peer_ref_count": int(
                                getattr(
                                    report, "bracket_peer_ref_count", 0,
                                ) or 0,
                            ),
                            "original_price_usd": original_total,
                            "proposed_price_usd": proposed_total,
                            "price_delta_usd": price_delta,
                            "n_priced_cards_original": original_priced,
                            "n_priced_cards_proposed": proposed_priced,
                            # EDHREC average-deck preview — mirrors the
                            # sync /api/audit endpoint. None when no
                            # average deck is available; the UI hides
                            # the <details> panel accordingly.
                            "average_deck_preview": project_average_deck_preview(
                                getattr(report, "average_deck", None),
                                getattr(report, "edhrec_categories", {}) or {},
                                original,
                            ),
                            # Salt-warning banner — mirrors the sync
                            # endpoint. None at B4/B5 or when no salt
                            # data is available.
                            "salt_warning": project_salt_warning(
                                original, salt_map, bracket,
                            ),
                            # Per-deck protected-cards list from
                            # [metadata] Protect= entries — same
                            # surface as the sync endpoint.
                            "protected_cards": read_protected_cards(
                                original,
                            ),
                            # Deck-health tile signals — mirrors the
                            # sync endpoint shape so the UI renderer
                            # is shared.
                            "deck_health": _compute_deck_health_safe(
                                original,
                            ),
                            # Combo/bracket assessment — mirrors the sync
                            # endpoint so the UI renderer is shared.
                            "combo_assessment": _assess_combos_safe(
                                original, bracket,
                            ),
                        })
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
            finally:
                # Always restore the BYO Anthropic key — the env mutation
                # must not linger across requests.
                if use_claude and byo_key:
                    if saved_key is None:
                        os.environ.pop("ANTHROPIC_API_KEY", None)
                    else:
                        os.environ["ANTHROPIC_API_KEY"] = saved_key

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
