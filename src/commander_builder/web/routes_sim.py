"""A/B simulation + iteration-log routes for the web layer.

Five routes live here, all centered on running Forge simulations
and persisting the audit→sim cycle to ``knowledge_log.sqlite``:

- ``POST /api/propose_swap`` — runs an A/B Forge comparison between
  the on-disk deck and a proposed variant. Returns the
  ``ComparisonReport`` as JSON. Synchronous; 5 games ≈ 15s in 1v1,
  20 games ≈ 60s, pod mode 4-6x slower.
- ``POST /api/save_iteration`` — persists one audit→sim row to
  ``knowledge_log.sqlite``. Returns ``{id, verdict, stats}``.
- ``GET /api/iteration/<id>`` — full iteration record including
  the deck_snapshot blob.
- ``GET /api/compare/<old_id>/<new_id>`` — card-level diff between
  two iteration snapshots.
- ``GET /api/iteration/<id>/snapshot`` — plain-text .dck snapshot
  for an iteration.

Built via ``make_sim_blueprint(deck_dir, knowledge_db,
resolve_deck_path)`` so route handlers close over the deck
directory + knowledge_log database path + the
``_resolve_deck_path`` helper from ``web/app.py``.

Extracted from ``web/app.py`` as part of the 2026-05-13 blueprint
refactor (tier-3 issue #3.1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from flask import Blueprint, Response, current_app, jsonify, request

from ..knowledge_log import (
    Iteration,
    decisive_win_rate,
    get_iteration,
    record_iteration,
    stats_summary,
)
from ._helpers import (
    _iteration_to_dict,
    _resolve_deck_path,
    _to_constructed_format,
)


def make_sim_blueprint(
    deck_dir: Path,
    knowledge_db: Optional[Path],
) -> Blueprint:
    """Build a Flask Blueprint for the A/B sim + iteration-log route
    group.

    Closes over ``deck_dir`` + ``knowledge_db``. ``_resolve_deck_path``
    is imported from ``_helpers.py`` directly (was a constructor
    parameter before the 2026-05-14 cleanup).
    """
    bp = Blueprint("sim", __name__)

    @bp.route("/api/propose_swap", methods=["POST"])
    def propose_swap():
        """Run an A/B Forge comparison between an existing deck
        (`deck_id`) and a modified version (`new_text`).

        Body: ``{"deck": "<id>", "new_text": "<.dck blob>",
                  "games": 5|10|20, "bracket": int=3, "mode": "1v1"|"pod"}``

        Synchronous — returns when Forge finishes (5 games is roughly
        15s in 1v1 mode; 20 games ~60s; pod mode is 4-6x slower).
        Returns the ComparisonReport as JSON on success.

        Forge availability is required. If ForgeRunner.locate() fails
        (no JRE, no vendor/forge, etc) the endpoint returns 503.
        """
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        deck_id = payload.get("deck")
        new_text = payload.get("new_text") or ""
        try:
            games = int(payload.get("games", 40))
        except (TypeError, ValueError):
            return jsonify({"error": "games must be int"}), 400
        if games not in (10, 40, 100):
            return jsonify({
                "error": "games must be one of 10, 40, 100",
            }), 400
        try:
            bracket = int(payload.get("bracket", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "bracket must be int"}), 400
        # Range check to match save_iteration: an out-of-range bracket
        # would propagate into compare()'s filler-deck selection and
        # produce a nonsense A/B baseline instead of failing fast.
        if bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be 1..5"}), 400
        # Default to pod (4-player commander with shared filler
        # opposition) because that's the honest commander signal —
        # 1v1 reduces commander to a duel and misses politics /
        # threat-assessment / archenemy dynamics. Pod also avoids
        # the deck-format conversion the constructed path requires.
        # Caller can override to '1v1' for fast goldfish-style runs.
        mode = payload.get("mode", "pod")
        if mode not in ("1v1", "pod"):
            return jsonify({
                "error": "mode must be '1v1' or 'pod'",
            }), 400

        old_path = _resolve_deck_path(deck_dir, deck_id, None)
        if old_path is None:
            return jsonify({"error": "old deck not found",
                            "deck": deck_id}), 404
        if not new_text.strip():
            return jsonify({"error": "new_text is empty"}), 400

        from ..compare_versions import compare, diff_deck_text

        # Quick dry-run: if no actual changes, refuse to spend Forge
        # cycles on a no-op.
        old_text = old_path.read_text(encoding="utf-8")
        diff = diff_deck_text(old_text, new_text)
        if not diff["added"] and not diff["removed"]:
            return jsonify({
                "error": "no changes detected",
                "diff": diff,
            }), 400

        # Stage the proposed deck. Two format-dependent destinations:
        # - mode='pod' (commander): siblings of the original under
        #   userdata/decks/commander/ so Forge's `-f commander` finds
        #   them.
        # - mode='1v1' (constructed): under userdata/decks/constructed/
        #   because Forge's `-f constructed` ONLY looks there. Staging
        #   1v1 decks in the commander folder produces "No deck found"
        #   errors and zero games — that was our 'Done. 0 games played'
        #   mystery. Strip the [USER]/[REF] prefix because Forge's CLI
        #   loader also chokes on filenames starting with `[`.
        from datetime import datetime as _dt
        from uuid import uuid4
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        # The timestamp alone has 1-SECOND granularity, and Flask serves
        # propose_swap concurrently (threaded, same process). Two A/B
        # sims on the same deck started within the same second would
        # build IDENTICAL staged paths: request B's write_text clobbers
        # the file request A's Forge JVM is mid-reading (wrong deck gets
        # simmed), and A's cleanup unlink deletes the file B's Forge
        # still needs (FileNotFound / "0 games played"). Append a short
        # random component to make every request's staged names unique.
        # uuid4 — NOT os.getpid(): concurrent requests share one process,
        # so the pid is identical exactly when it would need to differ.
        # The timestamp stays because it's human-friendly for triaging
        # leftover staged files (and the stale-file sweep keys on it).
        uid = uuid4().hex[:8]
        bare_stem = old_path.stem
        for _prefix in ("[USER] ", "[REF] "):
            if bare_stem.startswith(_prefix):
                bare_stem = bare_stem[len(_prefix):]
                break
        if mode == "1v1":
            stage_dir = old_path.parent.parent / "constructed"
            stage_dir.mkdir(parents=True, exist_ok=True)
        else:
            stage_dir = old_path.parent
        # Build the filename FROM staged_name (single source of truth)
        # so the Name= metadata below can never drift from the stem —
        # log_parser._normalize must map both to the same key for win
        # attribution to work. _normalize strips only the [USER] prefix,
        # the [B<n>] suffix and the .dck extension, so the _{ts}_{uid}
        # suffix survives identically on both sides of the comparison.
        staged_name = f"{bare_stem}_proposed_{ts}_{uid}"
        new_path = stage_dir / f"{staged_name}.dck"
        # Rewrite the [metadata] Name= field so Forge displays this
        # deck distinctly from the original. Without this both decks
        # report 'Name=Hakbal of the Surging Soul' in Forge's Match
        # Result lines and log_parser can't attribute wins to either
        # side — every game looks like a tie regardless of who won.
        # The rewrite logic itself lives in dck_meta (shared with the
        # snapshot / proposer / meta_test deck writers, which hit the
        # same misattribution).
        from ..dck_meta import rewrite_name
        new_text_staged = rewrite_name(new_text, staged_name)

        # When mode='1v1' the format is `constructed`, but the user's
        # decks are commander-format (have a [Commander] section).
        # Forge silently runs zero games when the deck shape doesn't
        # match the format flag. Convert both decks to constructed
        # before staging — this mirrors forge_py's
        # correlate_with_forge.py conversion pattern.
        if mode == "1v1":
            new_text_staged = _to_constructed_format(new_text_staged)

        try:
            new_path.write_text(new_text_staged, encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": f"could not stage new deck: {exc}"}), 500

        # The OLD deck file is the unchanged user deck — also has a
        # [Commander] section, also needs conversion for 1v1. Stage
        # a sibling _converted_<timestamp>_<uid>.dck holding the converted
        # text so we don't mutate the user's actual deck file.
        old_converted_path: Optional[Path] = None
        old_for_compare = old_path.name
        if mode == "1v1":
            old_text = old_path.read_text(encoding="utf-8")
            # Ensure the old deck's metadata Name= is also distinct
            # so log_parser can split wins between old + new. Reuse the
            # same per-request uid — the _proposed_/_converted_ infix
            # already separates the two sides within a request, and one
            # uid per request makes "which files belong together" obvious
            # when triaging leftovers.
            old_staged_name = f"{bare_stem}_converted_{ts}_{uid}"
            old_text = rewrite_name(old_text, old_staged_name)
            old_text = _to_constructed_format(old_text)
            old_converted_path = stage_dir / f"{old_staged_name}.dck"
            try:
                old_converted_path.write_text(old_text, encoding="utf-8")
                old_for_compare = old_converted_path.name
            except OSError as exc:
                return jsonify({
                    "error": f"could not stage converted old deck: {exc}",
                }), 500

        try:
            from ..forge_runner import ForgeRunner
            runner = ForgeRunner.locate()
        except Exception as exc:
            try:
                new_path.unlink()
            except OSError as cleanup_exc:
                print(
                    f"WARN: could not remove staged deck {new_path}: "
                    f"{cleanup_exc}", flush=True,
                )
            return jsonify({
                "error": "Forge not available",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 503

        try:
            from ..compare_versions import auto_filler_pairs
            report = compare(
                old_deck=old_for_compare,
                new_deck=new_path.name,
                bracket=bracket,
                games_per_pod=games,
                # Sprint 1E: scale filler pairs with CPU count so multi-
                # core hosts get tighter verdicts at the same wall-time.
                # 1v1 mode ignores this.
                filler_pairs=auto_filler_pairs(),
                mode=mode,
                runner=runner,
                # 1v1 mode stages files under userdata/decks/constructed/
                # so compare()'s file-existence checks need to look
                # there instead of the default commander dir.
                deck_dir=stage_dir,
            )
        except Exception as exc:  # pragma: no cover - Forge runtime errors
            for p in (new_path, old_converted_path):
                if p is None:
                    continue
                try:
                    p.unlink()
                except OSError as cleanup_exc:
                    print(
                        f"WARN: could not remove staged deck {p}: "
                        f"{cleanup_exc}", flush=True,
                    )
            return jsonify({
                "error": "compare failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 500

        # Track 2 prep — run the same A/B through forge_py.combat and
        # append a paired row to the correlation log. Opt-in via the
        # COMMANDER_BUILDER_CORRELATE_FORGE_PY=1 env var so the
        # default propose-swap path doesn't pay forge_py's wall-time
        # tax. Once the correlation log has enough rows to compute
        # Pearson r per archetype, we can flip the default and use
        # forge_py as a pre-filter.
        _correlate_flag = os.environ.get(
            "COMMANDER_BUILDER_CORRELATE_FORGE_PY", "",
        ).strip().lower()
        if _correlate_flag in ("1", "true", "yes"):
            try:
                from ..forge_py_correlation import (
                    run_forge_py_ab, log_correlation_row,
                )
                old_for_py = (
                    stage_dir / old_for_compare
                    if mode == "1v1" else old_path
                )
                new_for_py = stage_dir / new_path.name
                py_result = run_forge_py_ab(
                    old_for_py, new_for_py,
                    # Cap forge_py games at min(games, 5) — fast but not
                    # free, we only need a comparable signal.
                    games_per_pod=min(games, 5),
                    mode=mode,
                )
                log_path = deck_dir.parent.parent / "_forge_py_correlation.csv"
                log_correlation_row(
                    log_path,
                    old_deck=old_path.name,
                    new_deck=new_path.name,
                    bracket=bracket, mode=mode, games_per_pod=games,
                    forge_old_wins=report.old_stats.wins,
                    forge_new_wins=report.new_stats.wins,
                    forge_draws=report.draws,
                    forge_duration_sec=sum(
                        p.get("duration_sec", 0) for p in report.pods
                    ),
                    py_old_wins=py_result.old_wins,
                    py_new_wins=py_result.new_wins,
                    py_draws=py_result.draws,
                    py_duration_sec=py_result.duration_sec,
                    py_error=py_result.error,
                )
            except Exception as exc:  # noqa: BLE001
                # Never let the correlation harness break the user-facing
                # response. Just log so we notice it later.
                current_app.logger.warning(
                    "forge_py correlation harness failed: %s: %s",
                    type(exc).__name__, exc,
                )

        # Drop the staged proposed_*.dck and converted-old (1v1 mode
        # only) now that Forge has finished with them. They exist
        # only as working copies for the A/B sim; leaving them on
        # disk pollutes the sidebar and confuses future sessions.
        for p in (new_path, old_converted_path):
            if p is None:
                continue
            try:
                p.unlink()
            except OSError as cleanup_exc:
                # Stale _proposed_*.dck files are exactly what this
                # sweep exists to prevent — a silent failure here means
                # the disk quietly fills with working copies.
                print(
                    f"WARN: could not remove staged deck {p}: "
                    f"{cleanup_exc}", flush=True,
                )

        return jsonify({
            "old_deck": old_path.name,
            "new_deck": new_path.name,
            "diff": diff,
            "games_per_pod": games,
            "mode": mode,
            "bracket": bracket,
            "winner": report.winner,
            "old_wins": report.old_stats.wins,
            "new_wins": report.new_stats.wins,
            "old_games": report.old_stats.games,
            "new_games": report.new_stats.games,
            "draws": report.draws,
            "margin": report.margin,
            "total_games": report.total_games,
            "timestamp": report.timestamp,
            # Sprint 1B telemetry: tell the UI when adaptive early-stop
            # cut the run short so the user knows the verdict is robust
            # despite fewer pods running.
            "pods_completed": len(report.pods),
            "pods_planned": report.pods_planned or len(report.pods),
            "stopped_early": bool(report.stopped_early),
            # Pod-failure telemetry ("no silent failures"): crashed /
            # dead-timed-out pods are EXCLUDED from total_games by
            # compare(); tell the UI so a verdict built on fewer games
            # than requested isn't presented as a full-strength result.
            # getattr defaults keep this endpoint working with report
            # doubles (tests fake compare() with SimpleNamespace) and any
            # pre-fix report shape that lacks the failure fields.
            "failed_pods": getattr(report, "failed_pods", 0),
            "timed_out_pods": getattr(report, "timed_out_pods", 0),
            "excluded_games": getattr(report, "excluded_games", 0),
            "pod_failures": getattr(report, "pod_failures", []),
            # Sprint 1C telemetry: per-pod intra-pod abort summary so
            # the UI can show "Pod 2 stopped at game 3/5 (decisive)".
            "pod_summaries": [
                {
                    "pod_index": p.get("pod_index", i + 1),
                    "intra_pod_aborted": bool(p.get("intra_pod_aborted")),
                    "pod_failed": bool(p.get("pod_failed")),
                    "failure_reason": p.get("failure_reason"),
                    "games_actually_played": int(
                        p.get("games_actually_played") or 0
                    ),
                    "duration_sec": p.get("duration_sec", 0),
                }
                for i, p in enumerate(report.pods)
            ],
        })

    @bp.route("/api/save_iteration", methods=["POST"])
    def save_iteration():
        """Persist one audit→sim cycle to knowledge_log.sqlite.

        Body::

            {
                "deck_id": "<filename stem or Moxfield publicId>",
                "deck_name": "<display name>",
                "bracket": 1..5,
                "audit_version": "v3" (optional),
                "audit_manifest": {"added": [...], "removed": [...],
                                    "rationale": "..."} (optional),
                "sim_report": { ...full propose_swap response... } (optional),
                "verdict": "kept" | "reverted" | "neutral"
                           | "inconclusive" | "pending",
                "verdict_notes": "..." (optional),
                "deck_snapshot": "<.dck text>" (optional),
                "parent_id": int (optional)
            }

        Returns ``{"id": <new row id>, "stats": <stats_summary>}`` so the
        UI can show "Saved iteration #N — knowledge_log now has X rows."
        """
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        deck_id = (payload.get("deck_id") or "").strip()
        deck_name = (payload.get("deck_name") or "").strip()
        if not deck_id:
            return jsonify({"error": "deck_id is required"}), 400
        if not deck_name:
            deck_name = deck_id

        try:
            bracket = int(payload.get("bracket", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "bracket must be int"}), 400
        if bracket not in (1, 2, 3, 4, 5):
            return jsonify({"error": "bracket must be 1..5"}), 400

        verdict = (payload.get("verdict") or "pending").strip()
        # 'inconclusive' must be accepted here: the web UI *defaults* the
        # save-verdict radio to it whenever the sim had < 20 decisive games
        # (see app.js renderSaveIterationBlock), and both the PATCH verdict
        # endpoint and knowledge_log already treat it as valid. Rejecting it
        # made the UI's own default save path 400.
        if verdict not in ("kept", "reverted", "neutral", "inconclusive",
                           "pending"):
            return jsonify({
                "error": "verdict must be one of kept, reverted, neutral, "
                         "inconclusive, pending",
            }), 400

        audit_manifest = payload.get("audit_manifest")
        if audit_manifest is not None and not isinstance(audit_manifest, dict):
            return jsonify({"error": "audit_manifest must be an object"}), 400
        sim_report = payload.get("sim_report")
        if sim_report is not None and not isinstance(sim_report, dict):
            return jsonify({"error": "sim_report must be an object"}), 400

        # Optional pricing snapshot — feeds the cost-evolution chart.
        # Number type only (zero is a legal price); reject strings so
        # silent type drift doesn't poison the analytics later.
        total_price_usd = payload.get("total_price_usd")
        if total_price_usd is not None and not isinstance(
            total_price_usd, (int, float),
        ):
            return jsonify({
                "error": "total_price_usd must be a number",
            }), 400
        # bool is a subclass of int in Python; reject it explicitly so
        # `total_price_usd: true` doesn't silently land as 1.0.
        if isinstance(total_price_usd, bool):
            return jsonify({
                "error": "total_price_usd must be a number",
            }), 400
        # Negative price almost certainly means bad Scryfall data or
        # an upstream sign flip; accepting it silently would poison
        # the cost-evolution chart with nonsensical points.
        if total_price_usd is not None and total_price_usd < 0:
            return jsonify({
                "error": "total_price_usd must be non-negative",
            }), 400

        if total_price_usd is not None:
            from datetime import datetime as _dt, timezone as _tz
            if audit_manifest is None:
                audit_manifest = {}
            # Caller-supplied pricing wins — let downstream pipelines
            # (iteration_loop, future re-syncs) own the field when they
            # set it explicitly.
            if "pricing" not in audit_manifest:
                audit_manifest["pricing"] = {
                    "total_price_usd": float(total_price_usd),
                    "captured_at": _dt.now(_tz.utc).isoformat(),
                }

        # Pull win-rate / margin out of sim_report if present so the
        # row is queryable without parsing the JSON blob every time.
        #
        # Win-rate convention (2026-07-20, see knowledge_log's schema
        # docstring): wins / HEAD-TO-HEAD DECISIVE games via the shared
        # helper, where decisive = old_wins + new_wins — the games one of
        # the two compared versions actually won. BOTH payload shapes
        # (the /api/propose_swap response body carrying total_games, and
        # hand-built / legacy AB-shaped payloads without it) compute this
        # same denominator. The previous pass (611feff) used total_games
        # - draws when total_games was present — but that count includes
        # FILLER-won pod games the head-to-head pair can never win
        # (fillers take roughly half the games in a 4-player pod), so the
        # two shapes wrote rates ~2x apart for the same outcome, and this
        # writer's compare-shaped rows were incomparable with the
        # AB-shaped writers'. When decisive == 0 the helper returns None
        # and the columns stay NULL.
        win_rate_old = None
        win_rate_new = None
        margin = None
        if isinstance(sim_report, dict):
            try:
                old_w = int(sim_report.get("old_wins") or 0)
                new_w = int(sim_report.get("new_wins") or 0)
                decisive = old_w + new_w
                win_rate_old = decisive_win_rate(old_w, decisive)
                win_rate_new = decisive_win_rate(new_w, decisive)
                if "margin" in sim_report and sim_report["margin"] is not None:
                    margin = int(sim_report["margin"])
                else:
                    margin = new_w - old_w
            except (TypeError, ValueError):
                pass

        parent_id = payload.get("parent_id")
        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (TypeError, ValueError):
                return jsonify({"error": "parent_id must be int"}), 400

        # Default deck_snapshot to the current on-disk .dck file when
        # the caller didn't supply one explicitly. Lets the UI persist
        # iterations without round-tripping the deck text through the
        # browser.
        deck_snapshot = payload.get("deck_snapshot")
        if deck_snapshot is None:
            path = _resolve_deck_path(deck_dir, deck_id, None)
            if path is not None:
                try:
                    deck_snapshot = path.read_text(encoding="utf-8")
                except OSError:
                    deck_snapshot = None

        it = Iteration(
            deck_id=deck_id,
            deck_name=deck_name,
            bracket=bracket,
            audit_version=payload.get("audit_version"),
            audit_manifest=audit_manifest,
            sim_report=sim_report,
            verdict=verdict,
            verdict_notes=payload.get("verdict_notes"),
            win_rate_old=win_rate_old,
            win_rate_new=win_rate_new,
            margin=margin,
            parent_id=parent_id,
            deck_snapshot=deck_snapshot,
        )

        try:
            new_id = record_iteration(it, db_path=knowledge_db)
            stats = stats_summary(db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({
                "error": "could not save iteration",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 500

        return jsonify({
            "id": new_id,
            "verdict": verdict,
            "stats": stats,
        })

    @bp.route("/api/iteration/<int:iteration_id>")
    def iteration_detail(iteration_id: int):
        """Full iteration record including the deck_snapshot blob.
        Listed separately from /api/iterations so the listing endpoint
        can stay payload-small."""
        try:
            it = get_iteration(iteration_id, db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover - sqlite errors
            return jsonify({"error": str(exc)}), 500
        if it is None:
            return jsonify({"error": "iteration not found",
                            "id": iteration_id}), 404
        body = _iteration_to_dict(it)
        body["deck_snapshot"] = it.deck_snapshot
        body["sim_report"] = it.sim_report
        return jsonify(body)

    @bp.route("/api/compare/<int:old_id>/<int:new_id>")
    def compare_iterations(old_id: int, new_id: int):
        """Card-level diff between two iteration snapshots.

        Returns ``{old_id, new_id, added: [...], removed: [...],
        unchanged_count: int}``. Useful for the UI's swap-history
        view: "what actually changed between v2 and v3?"
        """
        from ..compare_versions import diff_deck_text
        try:
            old_it = get_iteration(old_id, db_path=knowledge_db)
            new_it = get_iteration(new_id, db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover
            return jsonify({"error": str(exc)}), 500

        if old_it is None or new_it is None:
            return jsonify({
                "error": "iteration not found",
                "old_id": old_id, "new_id": new_id,
            }), 404
        if not old_it.deck_snapshot or not new_it.deck_snapshot:
            return jsonify({
                "error": "snapshot missing on one of the iterations",
                "old_id": old_id, "new_id": new_id,
            }), 404

        diff = diff_deck_text(old_it.deck_snapshot, new_it.deck_snapshot)
        unchanged = int(diff["unchanged_count"][0]) if diff["unchanged_count"] else 0
        return jsonify({
            "old_id": old_id, "new_id": new_id,
            "added": diff["added"],
            "removed": diff["removed"],
            "unchanged_count": unchanged,
        })

    @bp.route("/api/iteration/<int:iteration_id>/snapshot")
    def iteration_snapshot(iteration_id: int):
        """Plain-text .dck snapshot for an iteration. Convenient for
        copy-paste back into Moxfield or for `commander-revert`."""
        try:
            it = get_iteration(iteration_id, db_path=knowledge_db)
        except Exception as exc:  # pragma: no cover
            return jsonify({"error": str(exc)}), 500
        if it is None or not it.deck_snapshot:
            return jsonify({"error": "snapshot not found",
                            "id": iteration_id}), 404
        return Response(it.deck_snapshot, mimetype="text/plain")

    return bp
