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

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

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


# ---------------------------------------------------------------------------
# Async sim jobs (recommendation #2 — 2026-07-21)
#
# WHY: propose_swap runs compare() synchronously in the Flask request
# handler. A pod-mode 40-game sim can hold an HTTP connection for hours;
# a browser or reverse-proxy read timeout then loses the response even
# though the _compare/*.json report was written to disk. Moving the long
# compare() onto a background thread lets the POST return a job id
# immediately, and the browser polls a cheap GET for the result — the
# connection is never held open across the sim.
#
# The registry is a plain module-level dict guarded by a Lock. It is
# process-global (survives across requests, shared by every worker thread
# in Flask's threaded dev server) but NOT persisted across a server
# restart — for a single-user local tool that is acceptable (documented).
# A *finished* sim additionally writes its result to disk (see
# _persist_job) so a reloaded page — or even a restarted server — can
# still fetch a done report.
# ---------------------------------------------------------------------------

# job_id -> {status, created_at, progress, report, error}. status is one
# of queued|running|done|failed. progress is None until compare() reports
# its first pod completion, then {"pods_done": int, "pods_total": int}.
_SIM_JOBS: dict[str, dict] = {}
# One lock guards every read/write of _SIM_JOBS. The critical sections are
# tiny (dict get/set/update) so a single coarse lock has no meaningful
# contention and is far easier to reason about than per-job locks — the
# worst case is a poll GET waiting microseconds behind a status update.
_SIM_JOBS_LOCK = threading.Lock()

# Subdirectory (under the commander deck dir) where finished jobs persist
# their final status as ``<job_id>.json``. Keyed by the job_id — the same
# uuid4-hex convention propose_swap already uses to uniquify staged decks —
# so a reloaded/​restarted client can re-attach a job by id. The _compare/
# report the sim itself writes is named after the deck stems + timestamp,
# NOT the job id, so it can't be looked up by job id directly; this tiny
# sidecar bridges job_id -> the response body we already built.
_SIM_JOBS_SUBDIR = "_sim_jobs"


class SimExecutionError(Exception):
    """Raised by :func:`_execute_swap` when compare() itself fails.

    Carries a human-readable detail string. Callers map it to either an
    HTTP 500 (synchronous endpoint) or a job ``status="failed"`` with the
    message (async worker) — the staged decks are always cleaned up before
    it propagates.
    """


def _new_job() -> str:
    """Register a fresh queued job and return its id.

    job_id is a uuid4 hex — the same convention propose_swap uses for its
    per-request staged-file uniquifier, so ids are collision-free even when
    two sims start in the same second.
    """
    job_id = uuid4().hex
    with _SIM_JOBS_LOCK:
        _SIM_JOBS[job_id] = {
            "status": "queued",
            # ISO-8601 UTC so a reloaded client can sort/age jobs.
            "created_at": datetime.now(timezone.utc).isoformat(),
            "progress": None,
            "report": None,
            "error": None,
        }
    return job_id


def _set_job(job_id: str, **fields) -> None:
    """Merge ``fields`` into a job's record under the lock.

    Silently no-ops if the job id is unknown (e.g. it was never registered
    or the process restarted) so a late progress callback from a thread can
    never raise KeyError and kill the worker."""
    with _SIM_JOBS_LOCK:
        rec = _SIM_JOBS.get(job_id)
        if rec is not None:
            rec.update(fields)


def _get_job(job_id: str) -> Optional[dict]:
    """Return a *copy* of a job's record, or None if unknown.

    A copy (not the live dict) so the caller can serialize it without
    holding the lock and without racing a concurrent worker update."""
    with _SIM_JOBS_LOCK:
        rec = _SIM_JOBS.get(job_id)
        return dict(rec) if rec is not None else None


def _job_file(deck_dir: Path, job_id: str) -> Path:
    return deck_dir / _SIM_JOBS_SUBDIR / f"{job_id}.json"


def _persist_job(deck_dir: Path, job_id: str) -> None:
    """Write a finished job's record to ``_sim_jobs/<job_id>.json``.

    Called once, when the job reaches done/failed. This is what makes a
    done report survive a page reload OR a server restart: the in-memory
    registry is gone after a restart, but _get_job_persisted can rebuild
    the record from this file. Best-effort — a failure to persist must not
    turn a successful sim into a failed one, so it only warns."""
    rec = _get_job(job_id)
    if rec is None:
        return
    try:
        path = _job_file(deck_dir, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stamp the id INTO the file too so a bare read is self-describing.
        payload = dict(rec)
        payload["job_id"] = job_id
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        print(
            f"WARN: could not persist sim job {job_id}: {exc}",
            flush=True,
        )


def _get_job_persisted(deck_dir: Path, job_id: str) -> Optional[dict]:
    """Load a finished job's record from disk, or None if absent/unreadable.

    The re-attach fallback: when a job id isn't in the in-memory registry
    (server was restarted, or this worker never saw it) but the sim had
    already finished, its sidecar json still holds the full response."""
    path = _job_file(deck_dir, job_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _prepare_swap(deck_dir: Path, payload: Optional[dict]):
    """Validate the propose-swap payload and stage the A/B decks on disk.

    This is the FAST, synchronous half of a propose-swap: everything that
    can fail with a client-visible 4xx/5xx *before* Forge burns any
    wall-time (bad games/bracket/mode, missing deck, no-op diff, staging
    write errors, Forge unavailable). It is deliberately split out of the
    route handler so BOTH the synchronous ``/api/propose_swap`` and the
    async ``/api/propose_swap_async`` run byte-identical validation +
    staging — the ONLY difference between the two endpoints is where
    :func:`_execute_swap` runs (request thread vs. background thread).

    Returns ``(ctx, None)`` on success, where ``ctx`` carries everything
    _execute_swap needs. Returns ``(None, (body, status))`` on failure,
    where ``body`` is the JSON error dict and ``status`` the HTTP code so
    the caller just does ``return jsonify(body), status``.
    """
    if payload is None:
        return None, ({"error": "expected JSON body"}, 400)

    deck_id = payload.get("deck")
    new_text = payload.get("new_text") or ""
    try:
        games = int(payload.get("games", 40))
    except (TypeError, ValueError):
        return None, ({"error": "games must be int"}, 400)
    if games not in (10, 40, 100):
        return None, ({"error": "games must be one of 10, 40, 100"}, 400)
    try:
        bracket = int(payload.get("bracket", 3))
    except (TypeError, ValueError):
        return None, ({"error": "bracket must be int"}, 400)
    # Range check to match save_iteration: an out-of-range bracket would
    # propagate into compare()'s filler-deck selection and produce a
    # nonsense A/B baseline instead of failing fast.
    if bracket not in (1, 2, 3, 4, 5):
        return None, ({"error": "bracket must be 1..5"}, 400)
    # Default to pod (4-player commander with shared filler opposition)
    # because that's the honest commander signal — 1v1 reduces commander
    # to a duel and misses politics / threat-assessment / archenemy
    # dynamics. Pod also avoids the deck-format conversion the constructed
    # path requires. Caller can override to '1v1' for fast goldfish runs.
    mode = payload.get("mode", "pod")
    if mode not in ("1v1", "pod"):
        return None, ({"error": "mode must be '1v1' or 'pod'"}, 400)

    old_path = _resolve_deck_path(deck_dir, deck_id, None)
    if old_path is None:
        return None, ({"error": "old deck not found", "deck": deck_id}, 404)
    if not new_text.strip():
        return None, ({"error": "new_text is empty"}, 400)

    from ..compare_versions import diff_deck_text

    # Quick dry-run: if no actual changes, refuse to spend Forge cycles on
    # a no-op.
    old_text = old_path.read_text(encoding="utf-8")
    diff = diff_deck_text(old_text, new_text)
    if not diff["added"] and not diff["removed"]:
        return None, ({"error": "no changes detected", "diff": diff}, 400)

    # Stage the proposed deck. Two format-dependent destinations:
    # - mode='pod' (commander): siblings of the original under
    #   userdata/decks/commander/ so Forge's `-f commander` finds them.
    # - mode='1v1' (constructed): under userdata/decks/constructed/ because
    #   Forge's `-f constructed` ONLY looks there. Staging 1v1 decks in the
    #   commander folder produces "No deck found" errors and zero games —
    #   that was our 'Done. 0 games played' mystery. Strip the [USER]/[REF]
    #   prefix because Forge's CLI loader also chokes on filenames starting
    #   with `[`.
    # Local `from datetime import datetime as _dt` (NOT the module-level
    # binding) so tests can freeze the clock by patching the datetime
    # module's `datetime` attribute — see
    # test_propose_swap_same_second_requests_stage_distinct_paths. A
    # module-level `datetime` name binds the real class at import time and
    # would ignore that patch.
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    # The timestamp alone has 1-SECOND granularity, and Flask serves these
    # concurrently (threaded, same process). Two A/B sims on the same deck
    # started within the same second would build IDENTICAL staged paths:
    # request B's write_text clobbers the file request A's Forge JVM is
    # mid-reading (wrong deck gets simmed), and A's cleanup unlink deletes
    # the file B's Forge still needs (FileNotFound / "0 games played").
    # Append a short random component to make every request's staged names
    # unique. uuid4 — NOT os.getpid(): concurrent requests share one
    # process, so the pid is identical exactly when it would need to
    # differ. The timestamp stays because it's human-friendly for triaging
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
    # Build the filename FROM staged_name (single source of truth) so the
    # Name= metadata below can never drift from the stem — log_parser
    # ._normalize must map both to the same key for win attribution to
    # work. _normalize strips only the [USER] prefix, the [B<n>] suffix and
    # the .dck extension, so the _{ts}_{uid} suffix survives identically on
    # both sides of the comparison.
    staged_name = f"{bare_stem}_proposed_{ts}_{uid}"
    new_path = stage_dir / f"{staged_name}.dck"
    # Rewrite the [metadata] Name= field so Forge displays this deck
    # distinctly from the original. Without this both decks report the same
    # Name= in Forge's Match Result lines and log_parser can't attribute
    # wins to either side — every game looks like a tie regardless of who
    # won. The rewrite logic lives in dck_meta (shared with the snapshot /
    # proposer / meta_test deck writers, which hit the same misattribution).
    from ..dck_meta import rewrite_name
    new_text_staged = rewrite_name(new_text, staged_name)

    # When mode='1v1' the format is `constructed`, but the user's decks are
    # commander-format (have a [Commander] section). Forge silently runs
    # zero games when the deck shape doesn't match the format flag. Convert
    # both decks to constructed before staging — mirrors forge_py's
    # correlate_with_forge.py conversion pattern.
    if mode == "1v1":
        new_text_staged = _to_constructed_format(new_text_staged)

    try:
        new_path.write_text(new_text_staged, encoding="utf-8")
    except OSError as exc:
        return None, ({"error": f"could not stage new deck: {exc}"}, 500)

    # The OLD deck file is the unchanged user deck — also has a [Commander]
    # section, also needs conversion for 1v1. Stage a sibling
    # _converted_<timestamp>_<uid>.dck holding the converted text so we
    # don't mutate the user's actual deck file.
    old_converted_path: Optional[Path] = None
    old_for_compare = old_path.name
    if mode == "1v1":
        old_text = old_path.read_text(encoding="utf-8")
        # Ensure the old deck's metadata Name= is also distinct so
        # log_parser can split wins between old + new. Reuse the same
        # per-request uid — the _proposed_/_converted_ infix already
        # separates the two sides within a request, and one uid per request
        # makes "which files belong together" obvious when triaging.
        old_staged_name = f"{bare_stem}_converted_{ts}_{uid}"
        old_text = rewrite_name(old_text, old_staged_name)
        old_text = _to_constructed_format(old_text)
        old_converted_path = stage_dir / f"{old_staged_name}.dck"
        try:
            old_converted_path.write_text(old_text, encoding="utf-8")
            old_for_compare = old_converted_path.name
        except OSError as exc:
            return None, (
                {"error": f"could not stage converted old deck: {exc}"},
                500,
            )

    # Locate Forge LAST (after staging) so an availability failure still
    # cleans up the file we just wrote. 503 == the deck was fine, Forge
    # just isn't installed.
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
        return None, (
            {
                "error": "Forge not available",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            503,
        )

    ctx = {
        "deck_dir": deck_dir,
        "old_path": old_path,
        "new_path": new_path,
        "old_converted_path": old_converted_path,
        "stage_dir": stage_dir,
        "old_for_compare": old_for_compare,
        "runner": runner,
        "diff": diff,
        "games": games,
        "bracket": bracket,
        "mode": mode,
    }
    return ctx, None


def _cleanup_staged(ctx: dict) -> None:
    """Drop the staged proposed_*.dck (and converted-old, 1v1 only) now
    that Forge has finished with them. They exist only as working copies
    for the A/B sim; leaving them on disk pollutes the sidebar and confuses
    future sessions. A silent failure here means the disk quietly fills
    with working copies — exactly what this sweep exists to prevent — so it
    warns loudly."""
    for p in (ctx["new_path"], ctx["old_converted_path"]):
        if p is None:
            continue
        try:
            p.unlink()
        except OSError as cleanup_exc:
            print(
                f"WARN: could not remove staged deck {p}: {cleanup_exc}",
                flush=True,
            )


def _execute_swap(
    ctx: dict, progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Run the (slow) Forge comparison for a prepared ``ctx`` and return
    the JSON-ready response body.

    This is the half that moves off the request thread in async mode. It is
    the SAME compare() call the synchronous endpoint always made — nothing
    about the simulation changes, only the thread it runs on. On a compare()
    failure it cleans up the staged decks and raises
    :class:`SimExecutionError`; the caller decides whether that becomes a
    500 or a job ``status="failed"``.

    ``progress_cb(pods_done, pods_total)`` is forwarded into compare() so a
    polling client can show 'running (pod 2/4)…'. It is optional and, when
    compare() does not support the seam, simply never fires — progress then
    stays coarse (running vs done), which the design explicitly allows.
    """
    # Import at call time (not module load) so the test suite's
    # monkeypatch of commander_builder.compare_versions.compare is picked
    # up — the fake compare replaces the module attribute, and re-reading
    # it here resolves to the patched object.
    from ..compare_versions import auto_filler_pairs, compare

    deck_dir = ctx["deck_dir"]
    old_path = ctx["old_path"]
    new_path = ctx["new_path"]
    stage_dir = ctx["stage_dir"]
    old_for_compare = ctx["old_for_compare"]
    diff = ctx["diff"]
    games = ctx["games"]
    bracket = ctx["bracket"]
    mode = ctx["mode"]

    # compare() gained an optional progress_cb seam (2026-07-21) that fires
    # once per completed pod. Older/faked compare()s may not accept it —
    # only pass the kwarg when we actually have a callback AND compare
    # advertises it, so a test double with a fixed signature isn't handed
    # an unexpected keyword argument.
    compare_kwargs = dict(
        old_deck=old_for_compare,
        new_deck=new_path.name,
        bracket=bracket,
        games_per_pod=games,
        # Sprint 1E: scale filler pairs with CPU count so multi-core hosts
        # get tighter verdicts at the same wall-time. 1v1 mode ignores it.
        filler_pairs=auto_filler_pairs(),
        mode=mode,
        runner=ctx["runner"],
        # 1v1 mode stages files under userdata/decks/constructed/ so
        # compare()'s file-existence checks need to look there instead of
        # the default commander dir.
        deck_dir=stage_dir,
    )
    if progress_cb is not None:
        compare_kwargs["progress_cb"] = progress_cb

    try:
        report = compare(**compare_kwargs)
    except TypeError as exc:
        # If the ONLY reason compare() rejected the call is the new
        # progress_cb kwarg (a test double with a frozen signature), retry
        # once without it rather than failing the whole sim. Any other
        # TypeError is a genuine failure and re-raised below.
        if progress_cb is not None and "progress_cb" in str(exc):
            compare_kwargs.pop("progress_cb", None)
            try:
                report = compare(**compare_kwargs)
            except Exception as exc2:  # pragma: no cover - Forge runtime
                _cleanup_staged(ctx)
                raise SimExecutionError(
                    f"{type(exc2).__name__}: {exc2}"
                ) from exc2
        else:
            _cleanup_staged(ctx)
            raise SimExecutionError(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # pragma: no cover - Forge runtime errors
        _cleanup_staged(ctx)
        raise SimExecutionError(f"{type(exc).__name__}: {exc}") from exc

    # Track 2 prep — run the same A/B through forge_py.combat and append a
    # paired row to the correlation log. Opt-in via the
    # COMMANDER_BUILDER_CORRELATE_FORGE_PY=1 env var so the default
    # propose-swap path doesn't pay forge_py's wall-time tax.
    _correlate_flag = os.environ.get(
        "COMMANDER_BUILDER_CORRELATE_FORGE_PY", "",
    ).strip().lower()
    if _correlate_flag in ("1", "true", "yes"):
        try:
            from ..forge_py_correlation import (
                log_correlation_row, run_forge_py_ab,
            )
            old_for_py = (
                stage_dir / old_for_compare if mode == "1v1" else old_path
            )
            new_for_py = stage_dir / new_path.name
            py_result = run_forge_py_ab(
                old_for_py, new_for_py,
                # Cap forge_py games at min(games, 5) — fast but not free,
                # we only need a comparable signal.
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

    _cleanup_staged(ctx)

    return {
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
        # Sprint 1B telemetry: tell the UI when adaptive early-stop cut the
        # run short so the user knows the verdict is robust despite fewer
        # pods running.
        "pods_completed": len(report.pods),
        "pods_planned": report.pods_planned or len(report.pods),
        "stopped_early": bool(report.stopped_early),
        # Pod-failure telemetry ("no silent failures"): crashed / dead-
        # timed-out pods are EXCLUDED from total_games by compare(); tell
        # the UI so a verdict built on fewer games than requested isn't
        # presented as a full-strength result. getattr defaults keep this
        # working with report doubles (tests fake compare() with
        # SimpleNamespace) and any pre-fix report shape.
        "failed_pods": getattr(report, "failed_pods", 0),
        "timed_out_pods": getattr(report, "timed_out_pods", 0),
        "excluded_games": getattr(report, "excluded_games", 0),
        "pod_failures": getattr(report, "pod_failures", []),
        # Sprint 1C telemetry: per-pod intra-pod abort summary so the UI can
        # show "Pod 2 stopped at game 3/5 (decisive)".
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
    }


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
                  "games": 10|40|100, "bracket": int=3, "mode": "1v1"|"pod"}``

        SYNCHRONOUS — blocks until Forge finishes (10 games ~30s in 1v1;
        pod mode 4-6x slower; a 40-game pod sim can run for many minutes).
        Returns the full ComparisonReport as JSON on success.

        This endpoint is deliberately KEPT for programmatic callers (the
        test suite, scripts, anything that wants the report in one shot).
        The browser UI has migrated to ``/api/propose_swap_async`` +
        ``/api/sim_job/<id>`` so a long sim can't lose its result to an
        HTTP read-timeout — see that endpoint's docstring for the rationale.

        Forge availability is required. If ForgeRunner.locate() fails
        (no JRE, no vendor/forge, etc) the endpoint returns 503.
        """
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        # _prepare_swap does ALL the validation + staging (identical to the
        # async path) and returns either a ready-to-run ctx or an
        # (error-body, status) pair.
        ctx, err = _prepare_swap(deck_dir, payload)
        if err is not None:
            body, status = err
            return jsonify(body), status
        try:
            # No progress_cb on the sync path: nobody is polling, the whole
            # report is returned in one response.
            result = _execute_swap(ctx)
        except SimExecutionError as exc:
            return jsonify({
                "error": "compare failed",
                "detail": str(exc),
            }), 500
        return jsonify(result)

    @bp.route("/api/propose_swap_async", methods=["POST"])
    def propose_swap_async():
        """Start an A/B Forge comparison as a BACKGROUND job and return
        immediately with ``{"job_id": "<hex>"}`` (HTTP 202).

        WHY this exists alongside the sync endpoint: a pod-mode 40-game sim
        can run for many minutes to hours. The sync endpoint holds the HTTP
        connection open the entire time, so a browser or reverse-proxy read
        timeout silently loses the response — the _compare/*.json report is
        on disk, but the UX is broken. Here the POST returns as soon as the
        decks are staged; the sim runs on a daemon thread and the client
        polls ``GET /api/sim_job/<job_id>``.

        Contract decision (2026-07-21): the sync ``/api/propose_swap``
        contract is UNCHANGED (it still returns the full report) because the
        test suite and any programmatic caller depend on that shape;
        changing it to return {job_id} would break them. So the async
        behavior is a NEW sibling endpoint and the UI migrates to it.

        Validation (bad games/bracket/mode, missing deck, no-op diff, Forge
        unavailable) happens SYNCHRONOUSLY here — those still return the
        usual 4xx/5xx immediately, before any job is created, so the client
        gets fast, precise errors. Only the long compare() moves to the
        background.
        """
        payload = request.get_json(silent=True)
        # Validate + stage on the request thread so real client errors come
        # back as proper HTTP status codes, NOT as a job that immediately
        # reports failed. Only a *staged, Forge-available* request ever
        # becomes a job.
        ctx, err = _prepare_swap(deck_dir, payload)
        if err is not None:
            body, status = err
            return jsonify(body), status

        job_id = _new_job()

        def _worker():
            # This runs on a background daemon thread. EVERY path must land
            # the job in a terminal state (done/failed) — a thread that dies
            # without updating the registry would leave the client polling a
            # forever-"running" job. Hence the broad except at the bottom.
            _set_job(job_id, status="running")

            def _progress(pods_done: int, pods_total: int) -> None:
                # Coarse-but-honest progress: compare() calls this once per
                # completed pod. Surfaced to the poller as "pod 2/4".
                _set_job(job_id, progress={
                    "pods_done": pods_done,
                    "pods_total": pods_total,
                })

            try:
                result = _execute_swap(ctx, progress_cb=_progress)
                _set_job(job_id, status="done", report=result)
            except SimExecutionError as exc:
                # compare() failed — staged files already cleaned up inside
                # _execute_swap. Record the message; no 500, no dead thread.
                _set_job(job_id, status="failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001 - never die silently
                # Any other unexpected error (staging race, disk, a bug).
                # Best-effort cleanup so we don't leak staged decks, then
                # record the failure.
                try:
                    _cleanup_staged(ctx)
                except Exception:  # pragma: no cover - defensive
                    pass
                _set_job(
                    job_id, status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                # Persist the terminal record so a reloaded page (or a
                # restarted server) can still GET this job's result.
                _persist_job(deck_dir, job_id)

        # daemon=True: a background sim must never keep the process alive at
        # shutdown. Losing an in-flight job on server restart is acceptable
        # for a single-user local tool (documented); a job that already
        # FINISHED is recoverable from its persisted sidecar.
        threading.Thread(
            target=_worker, name=f"sim-{job_id[:8]}", daemon=True,
        ).start()
        return jsonify({"job_id": job_id}), 202

    @bp.route("/api/sim_job/<job_id>", methods=["GET"])
    def sim_job(job_id: str):
        """Poll a background sim job's status.

        GET (exempt from the JSON content-type gate, which only guards
        mutating methods). Returns the job record::

            {"job_id", "status": queued|running|done|failed,
             "created_at", "progress": {pods_done, pods_total}|null,
             "report": {...}|null, "error": str|null}

        ``report`` is the exact body ``/api/propose_swap`` would have
        returned, embedded once ``status == "done"``.

        Re-attach: if the id isn't in the in-memory registry (server was
        restarted, or a stale tab) we fall back to the persisted
        ``_sim_jobs/<id>.json`` sidecar so a finished sim's report is still
        fetchable. Unknown id (neither in memory nor on disk) -> 404.
        """
        rec = _get_job(job_id)
        if rec is None:
            # Re-attach fallback: the job may have finished before a server
            # restart wiped the in-memory registry.
            rec = _get_job_persisted(deck_dir, job_id)
        if rec is None:
            return jsonify({"error": "job not found", "job_id": job_id}), 404
        body = dict(rec)
        body["job_id"] = job_id
        return jsonify(body)


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
