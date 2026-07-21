"""Deck-editing + deck-info routes for the web layer.

Seven routes live here, all centered on reading/writing .dck
files and answering questions about deck contents:

- ``/api/deck_text``               (GET / PUT / DELETE)
- ``/api/import_deck``             (POST — Moxfield URL or paste)
- ``/api/deck_source``             (GET / PUT — Moxfield= metadata)
- ``/api/verify_against_source``   (GET — diff vs. live Moxfield)
- ``/api/moxfield_format``         (GET — Moxfield-paste-ready text)
- ``/api/game_changers``           (GET — Wizards' GC list)
- ``/api/deck_audit``              (GET — legality + GC scan)

Built via ``make_decks_blueprint(deck_dir, resolve_deck_path)``.

Extracted from ``web/app.py`` as part of the 2026-05-13 blueprint
refactor (tier-3 issue #3.1).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from flask import Blueprint, jsonify, request

from ._helpers import (
    _bracket_from_filename,
    _normalize_pasted_deck,
    _resolve_deck_path,
)


_BRACKET_NAMES = {
    1: "Exhibition", 2: "Core", 3: "Upgraded", 4: "Optimized", 5: "cEDH",
}


# ---------------------------------------------------------------------------
# FP-014.4 — async "Build from scratch" jobs.
#
# WHY ASYNC (the timing decision, documented):
#   build_deck (deck_builder._assemble) is NOT a cheap call. Its critical
#   path is:
#     1. fetch_average_deck / fetch_commander_page — one or two live EDHREC
#        HTTP round-trips (seconds each, and slower on a cold cache);
#     2. the lift stage — reads/builds the deck-corpus synergy matrix;
#     3. the bracket-steer loop — re-renders the deck and re-estimates the
#        bracket on EACH swap iteration, and every estimate resolves cards
#        through Scryfall (more cached I/O per card).
#   Individually any one is fine; stacked, a real build routinely exceeds the
#   3-5s a synchronous POST can safely hold before a browser or reverse-proxy
#   read-timeout risks silently dropping the response — the exact failure the
#   propose-swap async migration (#43) was built to avoid. So the build runs
#   on a background thread: POST returns a job_id immediately, the browser
#   polls a cheap GET. This mirrors routes_sim's sim-job contract.
#
# WHY A SEPARATE REGISTRY (not routes_sim's generalized one):
#   The sim-job registry carries sim-specific machinery (a disk sidecar so a
#   done REPORT survives a server restart, pod-progress plumbing) and a large
#   test surface. A build is short-lived and its result is a freshly-written
#   .dck the dashboard reloads anyway — page-reload recovery buys nothing, so
#   there's no disk sidecar here. Keeping this registry independent means the
#   sim-job tests stay untouched and green, and neither feature's failure mode
#   can leak into the other. It's the same shape (a Lock-guarded module dict),
#   just leaner.
#
# The registry is process-global (shared across Flask's threaded worker
# threads) but NOT persisted across a restart — acceptable for a single-user
# local tool, and documented.
# ---------------------------------------------------------------------------

# job_id -> {status, created_at, result, error}. status is one of
# queued|running|done|failed. ``result`` is None until done, then the JSON
# body the poll returns (deck id/name + the BuildResult summary).
_BUILD_JOBS: dict[str, dict] = {}
# One coarse lock guards every read/write of _BUILD_JOBS. Critical sections
# are tiny dict ops, so there's no meaningful contention — a poll GET waits
# microseconds at most behind a status update.
_BUILD_JOBS_LOCK = threading.Lock()


def _new_build_job() -> str:
    """Register a fresh queued build job and return its uuid4-hex id."""
    job_id = uuid4().hex
    with _BUILD_JOBS_LOCK:
        _BUILD_JOBS[job_id] = {
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": None,
            "error": None,
        }
    return job_id


def _set_build_job(job_id: str, **fields) -> None:
    """Merge ``fields`` into a build job's record under the lock.

    No-ops on an unknown id so a late thread update can never KeyError and
    kill the worker (same guard as routes_sim._set_job)."""
    with _BUILD_JOBS_LOCK:
        rec = _BUILD_JOBS.get(job_id)
        if rec is not None:
            rec.update(fields)


def _get_build_job(job_id: str) -> Optional[dict]:
    """Return a COPY of a build job's record, or None if unknown. A copy so
    the caller can serialize it without holding the lock or racing a worker."""
    with _BUILD_JOBS_LOCK:
        rec = _BUILD_JOBS.get(job_id)
        return dict(rec) if rec is not None else None


def _build_summary(result) -> dict:
    """Project a ``deck_builder.BuildResult`` into the JSON summary the UI
    renders after a build.

    Keeps the wire shape flat and self-describing: the manabase block carries
    the per-color have/target source counts (so the UI can show fixing
    coverage per color, not just a land total), and the personalization
    provenance (lift swaps, bracket estimate-vs-target, owned swaps, buy-list)
    is surfaced so the "building… done" state can explain WHAT the build did,
    not merely THAT it finished. All fields degrade to empty/zero when a stage
    was disabled or made no change, mirroring the CLI's summary."""
    mb = result.manabase
    # human-readable per-color "have/target" pairs, WUBRG-ordered, only for
    # colors the manabase actually targets (mono-color decks stay terse).
    coverage = {
        c: {"have": mb.sources.get(c, 0), "target": mb.targets.get(c, 0)}
        for c in "WUBRG" if c in mb.targets
    }
    return {
        "source": result.source,
        "colors": result.colors,
        "nonland_count": result.nonland_count,
        "land_count": result.land_count,
        "dropped_off_color": result.dropped_off_color,
        "manabase": {
            "land_count": mb.land_count,
            "fixing_land_count": mb.fixing_land_count,
            "basic_count": mb.basic_count,
            "kept_seed_lands": mb.kept_seed_lands,
            "degraded": mb.degraded,
            "coverage": coverage,
        },
        "bracket_target": result.bracket_target,
        "bracket_estimate": result.bracket_estimate,
        "lift_swaps": result.lift_swaps,
        "lift_skipped": result.lift_skipped,
        "steer_notes": result.steer_notes,
        "owned_swaps": result.owned_swaps,
        "buy_list": result.buy_list,
    }


def make_decks_blueprint(deck_dir: Path) -> Blueprint:
    """Build a Flask Blueprint for the deck-edit / deck-info route
    group.

    Closes over ``deck_dir``. ``_resolve_deck_path`` is imported
    from ``_helpers.py`` directly (was a constructor parameter
    before the 2026-05-14 cleanup).
    """
    bp = Blueprint("decks", __name__)

    @bp.route("/api/deck_text", methods=["GET", "PUT", "DELETE"])
    def deck_text():
        """Read / overwrite / delete a .dck file by id.

        - GET   → {"deck", "path", "text"} for the file
        - PUT   body {"text": ...} overwrites in place
        - DELETE removes the file
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({
                "error": "deck not found",
                "deck": deck_id, "path": explicit,
            }), 404

        if request.method == "GET":
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                return jsonify({"error": str(exc)}), 500
            return jsonify({
                "deck": deck_id,
                "path": str(path),
                "text": text,
            })

        if request.method == "PUT":
            # silent=True (not force=True): the app-level before_request
            # gate already guarantees Content-Type: application/json for
            # mutating methods, so parsing honors the header; a malformed
            # body surfaces as None -> 400 rather than an exception.
            payload = request.get_json(silent=True)
            if payload is None:
                return jsonify({"error": "expected JSON body"}), 400
            new_text = payload.get("text") or ""
            if not new_text.strip():
                return jsonify({"error": "text is empty"}), 400
            try:
                path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                return jsonify({"error": str(exc)}), 500
            return jsonify({"deck": deck_id, "path": str(path),
                            "saved": True})

        # DELETE
        try:
            path.unlink()
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"deck": deck_id, "deleted": True})

    @bp.route("/api/import_deck", methods=["POST"])
    def import_deck():
        """Create a new .dck under deck_dir from either a Moxfield URL
        or a paste of the deck text.

        Body: ``{"name": "<display>", "moxfield_url": "<url>"}``  OR
              ``{"name": "<display>", "paste_text": "<.dck, MTGA
              export, CSV, or plain card lines>"}``

        Filename is derived from ``name`` with a ``[USER]`` prefix and
        ``[B?]`` suffix so the deck shows up in the user-only sidebar.
        """
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        name = (payload.get("name") or "").strip()
        url = (payload.get("moxfield_url") or "").strip()
        paste = (payload.get("paste_text") or "").strip()
        # Bracket must be a valid Commander bracket (1..5). The value
        # is baked into the filename as a "[B<n>]" suffix, and
        # ``_bracket_from_filename`` downstream only recognizes
        # single-digit 1..5 — an unvalidated "[B9]" (or "[B-1]")
        # filename would silently break bracket resolution for the
        # deck's whole lifetime. Absent/empty means "not specified"
        # and defaults to 3 (Upgraded); an explicitly-provided bad
        # value is a client error and gets a 400.
        bracket_raw = payload.get("bracket")
        if bracket_raw in (None, ""):
            bracket = 3
        else:
            try:
                bracket = int(bracket_raw)
            except (TypeError, ValueError):
                return jsonify({
                    "error": "bracket must be an integer 1..5",
                }), 400
            if bracket not in (1, 2, 3, 4, 5):
                return jsonify({
                    "error": "bracket must be an integer 1..5",
                }), 400

        if not url and not paste:
            return jsonify({
                "error": "need moxfield_url or paste_text",
            }), 400

        deck_text_out: str
        derived_name = name
        if url:
            try:
                from ..moxfield_import import (
                    fetch_deck, parse_deck_id, to_dck,
                )
                public_id = parse_deck_id(url)
                deck_json = fetch_deck(public_id)
                deck_text_out = to_dck(deck_json)
                if not derived_name:
                    derived_name = deck_json.get("name", "Imported")
            except Exception as exc:
                return jsonify({
                    "error": "Moxfield fetch failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }), 502
        else:
            # Paste path. `_normalize_pasted_deck` auto-detects and
            # accepts .dck format (with [Main] sections), MTGA/Arena
            # exports, CSV card lists, and the plain Moxfield
            # bulk-paste line list — all converge on the same .dck
            # intermediate the writers below consume.
            from ..import_formats import ImportFormatError
            try:
                deck_text_out = _normalize_pasted_deck(paste)
            except ImportFormatError as exc:
                # A paste positively detected as Arena/CSV had a bad
                # line. The error message names the offending line —
                # surface it as a client error, never a stacktrace.
                return jsonify({
                    "error": "could not parse pasted deck list",
                    "detail": str(exc),
                }), 400
            if not derived_name:
                derived_name = "Pasted Deck"

        # Filename: [USER] <name> [B<bracket>].dck. Sanitize invalid
        # path chars (keep brackets — they're meaningful here).
        import re as _re
        safe = _re.sub(r"[<>:\"/\\|?*]", "_", derived_name)
        safe = safe.strip().strip(".")
        if not safe.lower().startswith("[user]"):
            safe = f"[USER] {safe}"
        if not _re.search(r"\[B\d\]$", safe):
            safe = f"{safe} [B{bracket}]"
        filename = f"{safe}.dck"
        target = deck_dir / filename

        if target.exists():
            return jsonify({
                "error": "deck with this name already exists",
                "filename": filename,
            }), 409
        # Stamp Name= from the final filename stem so Forge's deck picker
        # and every name-keyed pipeline (compare_versions, pool_curator)
        # can map this file back to itself even when the sanitizer above
        # rewrote characters the Moxfield name contained (':', emoji, ...).
        # The pretty name is preserved as DisplayName= for the status CLI;
        # bare pastes with no Name= get one synthesized from the stem.
        from ..dck_meta import stamp_name_preserving_display
        deck_text_out = stamp_name_preserving_display(
            deck_text_out, target.stem,
        )
        try:
            # Defensive: a fresh checkout or first-run-after-config-change
            # may not have created the deck dir yet. Create parents so
            # the import doesn't fail with ENOENT on a missing parent.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(deck_text_out, encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "id": target.stem,
            "name": _re.sub(r"^\[USER\]\s*", "", target.stem),
            "filename": filename,
            "path": str(target),
        })

    @bp.route("/api/build_deck", methods=["POST"])
    def build_deck_route():
        """FP-014.4 — build a legal 99 from a commander + target bracket.

        Body::

            {"commander": "Krenko, Mob Boss", "bracket": 3,
             "options": {"no_lift": false, "no_steer": false,
                         "owned_bias": true}}

        ASYNC (see the module-level rationale): a real build hits EDHREC plus
        the lift/steer loops and can outlast a synchronous POST's safe
        connection window. So validation happens HERE, on the request thread
        (bad commander/bracket -> immediate 4xx), and only the long
        _assemble() call moves to a background thread. Returns ``{"job_id"}``
        (HTTP 202); the client polls ``GET /api/build_job/<id>``.

        The written .dck goes through the SAME contract the import path uses —
        ``deck_dir / f"{result.stem}.dck"`` where the stem is
        ``[USER] <name> [B<n>]`` and ``Name=`` is already stamped to match it
        (deck_builder stamps it) — so the dashboard sidebar, bracket
        resolution, and the improve loop all accept the output unchanged.
        """
        # silent=True: the app-wide before_request gate already guarantees
        # Content-Type: application/json for POST, so a malformed body
        # surfaces as None -> 400 rather than an exception.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        commander = (payload.get("commander") or "").strip()
        if not commander:
            return jsonify({"error": "commander is required"}), 400

        # Bracket 1..5. Absent/empty defaults to 3 (Upgraded) — matches the
        # import route and the CLI default. An explicitly-bad value is a
        # client error (a nonsense bracket would steer the build into a
        # meaningless target), so 400 it rather than silently coercing.
        bracket_raw = payload.get("bracket")
        if bracket_raw in (None, ""):
            bracket = 3
        else:
            try:
                bracket = int(bracket_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "bracket must be an integer 1..5"}), 400
            if bracket not in (1, 2, 3, 4, 5):
                return jsonify({"error": "bracket must be an integer 1..5"}), 400

        # Options: each toggle is a bool with a build-appropriate default.
        # Personalization is the POINT of a from-scratch build, so lift + steer
        # are ON unless the user opts out; owned-bias is ON but only bites when
        # a collection is actually registered (resolved in the worker).
        opts = payload.get("options") or {}
        if not isinstance(opts, dict):
            return jsonify({"error": "options must be an object"}), 400
        no_lift = bool(opts.get("no_lift", False))
        no_steer = bool(opts.get("no_steer", False))
        owned_bias = bool(opts.get("owned_bias", True))
        # Optional display name; defaults inside _assemble to "<commander>
        # Build". Kept here so the UI can offer a rename field later without
        # an API change.
        display_name = (payload.get("name") or "").strip() or None

        job_id = _new_build_job()

        def _worker():
            # Runs on a background daemon thread. EVERY path MUST land the job
            # in a terminal state (done/failed) — a thread that died without
            # updating the registry would leave the client polling forever.
            _set_build_job(job_id, status="running")
            try:
                # Import at call time (not module load) so a test's
                # monkeypatch of commander_builder.deck_builder._assemble is
                # honored — re-reading the attribute here resolves the patched
                # object. _assemble (not build_deck) because we need the full
                # BuildResult for the summary, not just the .dck text.
                from .. import deck_builder
                from .. import collection as collection_store

                # Owned-bias only means anything with a registered collection.
                # load_collection() returns None when unconfigured, so pass a
                # path only when one exists AND the toggle is on — otherwise
                # the whole owned-aware stage (and its buy-list) stays inert,
                # exactly as --no-owned-bias would leave it.
                coll_path = None
                if owned_bias and collection_store.load_collection() is not None:
                    coll_path = collection_store.collection_path()

                result = deck_builder._assemble(
                    commander,
                    bracket,
                    coll_path,
                    name=display_name,
                    enable_lift=not no_lift,
                    enable_steer=not no_steer,
                    owned_bias=owned_bias,
                    # Read the lift corpus from the same dir we write into —
                    # the pool the harvester/improve loop share (mirrors the
                    # commander-build CLI).
                    deck_dir=deck_dir,
                )

                # Write via the import path's invariant: stem-named file, Name=
                # already stamped to the stem inside _assemble. Overwrite is
                # allowed — a from-scratch build is an explicit regenerate, and
                # the deterministic stem means "build Krenko B3 again" should
                # refresh, not 409.
                deck_dir.mkdir(parents=True, exist_ok=True)
                target = deck_dir / f"{result.stem}.dck"
                target.write_text(result.text, encoding="utf-8")

                import re as _re
                _set_build_job(job_id, status="done", result={
                    "id": target.stem,
                    "name": _re.sub(r"^\[USER\]\s*", "", target.stem),
                    "filename": target.name,
                    "path": str(target),
                    "summary": _build_summary(result),
                })
            except ValueError as exc:
                # Clean, user-facing build error (e.g. "cannot build: no
                # EDHREC data for <commander>", or a bad bracket that slipped
                # past validation). Surface the message, no stacktrace.
                _set_build_job(job_id, status="failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001 — never die silently
                _set_build_job(
                    job_id, status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        # daemon=True: a build thread must never keep the process alive at
        # shutdown. Losing an in-flight build on restart is acceptable (the
        # user just rebuilds); nothing persistent is at stake.
        threading.Thread(
            target=_worker, name=f"build-{job_id[:8]}", daemon=True,
        ).start()
        return jsonify({"job_id": job_id}), 202

    @bp.route("/api/build_job/<job_id>", methods=["GET"])
    def build_job(job_id: str):
        """Poll a background build job's status.

        GET (exempt from the JSON content-type gate, which only guards
        mutating methods). Returns::

            {"job_id", "status": queued|running|done|failed,
             "created_at", "result": {...}|null, "error": str|null}

        ``result`` is the deck id/name + BuildResult summary, embedded once
        ``status == "done"``. Unknown id -> 404. Unlike sim jobs there's no
        disk re-attach: a build is short-lived and its output is the .dck
        itself, so a lost in-memory record just means "rebuild".
        """
        rec = _get_build_job(job_id)
        if rec is None:
            return jsonify({"error": "job not found", "job_id": job_id}), 404
        body = dict(rec)
        body["job_id"] = job_id
        return jsonify(body)

    @bp.route("/api/bulk_import", methods=["POST"])
    def bulk_import_route():
        """Import many Moxfield URLs at once.

        Body: ``{"urls": ["https://moxfield.com/decks/a", ...],
                 "is_user": true}``

        Wraps the same ``bulk_import`` library function the
        ``commander-bulk-import`` CLI uses, so the politeness contract
        (FETCH_SLEEP_SEC between requests) and dedup behavior are
        shared. Response is the JSON form of ``BulkImportResult``:

            {
              "successes": [{url, deck_id, path}, ...],
              "duplicates": [{url, deck_id, existing_path, reason}, ...],
              "failures":  [{url, error}, ...],
              "total": int, "success_count": int,
              "duplicate_count": int, "failure_count": int
            }

        Status code is 200 even with partial failures — the per-URL
        outcomes tell the UI what to render. 400 only on bad input.
        502 when EVERY url failed (likely network down).
        """
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        urls = payload.get("urls")
        if not isinstance(urls, list):
            return jsonify({
                "error": "urls must be a list of strings",
            }), 400

        is_user = bool(payload.get("is_user", True))

        # Hard cap to keep the request bounded — a 1000-URL paste at
        # 1s/fetch would block the worker for ~17 min. Surface the
        # cap to the client rather than silently truncating.
        MAX_BULK_URLS = 50
        if len(urls) > MAX_BULK_URLS:
            return jsonify({
                "error": f"too many urls — max {MAX_BULK_URLS} per request",
                "received": len(urls),
            }), 400

        from ..moxfield_import import bulk_import as _bulk_import
        result = _bulk_import(urls, out_dir=deck_dir, is_user=is_user)

        # If at least one URL was provided AND every one failed, surface
        # 502 so the UI can warn about network rather than just showing
        # an empty success table.
        if result.total > 0 and result.failure_count == result.total:
            return jsonify(result.to_dict()), 502
        return jsonify(result.to_dict())

    @bp.route("/api/deck_source", methods=["GET", "PUT"])
    def deck_source():
        """Get or set the Moxfield source URL attached to a deck.

        The deck's source URL is stored as a ``Moxfield=<publicId>``
        line in the [metadata] section of the .dck file (same shape
        moxfield_import already writes). This endpoint lets the UI:

        - GET: read the current source URL (None if unattached)
        - PUT: attach / update the source URL for an already-imported
          deck so future "verify against source" workflows can diff
          our local copy against the live Moxfield deck.
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500

        import re as _re
        existing = _re.search(r"^Moxfield=(.+)$", text, _re.MULTILINE)

        if request.method == "GET":
            mox_id = existing.group(1).strip() if existing else None
            return jsonify({
                "deck": deck_id,
                "moxfield_id": mox_id,
                "moxfield_url": (
                    f"https://moxfield.com/decks/{mox_id}"
                    if mox_id else None
                ),
            })

        # PUT — update / clear the Moxfield URL.
        # silent=True (not force=True): the app-level before_request gate
        # already guarantees Content-Type: application/json here, so
        # parsing honors the header; malformed JSON -> None -> 400.
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "expected JSON body"}), 400

        url = (payload.get("moxfield_url") or "").strip()
        if url:
            try:
                from ..moxfield_import import parse_deck_id
                mox_id = parse_deck_id(url)
            except Exception as exc:
                return jsonify({
                    "error": "could not parse Moxfield URL",
                    "detail": str(exc),
                }), 400
            new_meta = f"Moxfield={mox_id}"
            if existing:
                text = _re.sub(
                    r"^Moxfield=.+$", new_meta, text, count=1,
                    flags=_re.MULTILINE,
                )
            else:
                # Insert under [metadata]; create the section if missing.
                if "[metadata]" in text.lower():
                    text = _re.sub(
                        r"(\[metadata\][^\n]*\n)",
                        rf"\1{new_meta}\n", text, count=1,
                        flags=_re.IGNORECASE,
                    )
                else:
                    text = f"[metadata]\n{new_meta}\n\n" + text
            path.write_text(text, encoding="utf-8")
            return jsonify({
                "deck": deck_id, "moxfield_id": mox_id,
                "moxfield_url": f"https://moxfield.com/decks/{mox_id}",
            })

        # Empty URL → clear the metadata line.
        if existing:
            text = _re.sub(
                r"^Moxfield=.+\n?", "", text, count=1, flags=_re.MULTILINE,
            )
            path.write_text(text, encoding="utf-8")
        return jsonify({
            "deck": deck_id, "moxfield_id": None, "moxfield_url": None,
        })

    @bp.route("/api/verify_against_source")
    def verify_against_source():
        """Diff the local deck against the live Moxfield deck it was
        imported from. Surfaces any drift so the user can sync back.

        Returns:
            {
              "deck": "<id>",
              "source_url": "...",
              "in_local_only": [...],   # cards in our copy, not Moxfield
              "in_remote_only": [...],  # cards on Moxfield, not local
              "matched": int,
              "commander_changed": bool,  # [Commander] section drifted
              "local_commanders": [...],  # names in our [Commander]
              "remote_commanders": [...], # names in Moxfield's
            }

        The commander_* fields exist because ``diff_deck_text`` only
        reads the [Main] section — a commander swap on Moxfield (e.g.
        the user re-helmed the deck) used to report "no drift" here,
        which is exactly the change that invalidates every sim result
        and EDHREC recommendation downstream. Additive fields only, so
        pre-existing consumers of the response keep working.
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        import re as _re
        m = _re.search(r"^Moxfield=(.+)$", text, _re.MULTILINE)
        if not m:
            return jsonify({
                "error": "no Moxfield source attached",
                "hint": "PUT /api/deck_source first",
            }), 400
        mox_id = m.group(1).strip()
        try:
            from ..moxfield_import import fetch_deck, to_dck
            deck_json = fetch_deck(mox_id)
            remote_text = to_dck(deck_json)
        except Exception as exc:
            return jsonify({
                "error": "Moxfield fetch failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 502

        from ..compare_versions import diff_deck_text
        diff = diff_deck_text(text, remote_text)

        # diff_deck_text is [Main]-only by design (it diffs the 99),
        # so compare the [Commander] section separately. Reuses
        # intent's parser — it already strips qty prefixes and
        # ``|SET|CN`` edition suffixes, so a reprint on Moxfield
        # doesn't false-positive as a commander swap; only a NAME
        # change does. Compare as case-folded sets: partner
        # commanders can legitimately appear in either order, and
        # to_dck vs. our importer may disagree on casing.
        from ..intent import _parse_commander_names
        local_commanders = _parse_commander_names(text)
        remote_commanders = _parse_commander_names(remote_text)
        commander_changed = (
            {c.casefold() for c in local_commanders}
            != {c.casefold() for c in remote_commanders}
        )

        return jsonify({
            "deck": deck_id,
            "source_url": f"https://moxfield.com/decks/{mox_id}",
            "in_local_only": diff["removed"],
            "in_remote_only": diff["added"],
            "matched": int(diff["unchanged_count"][0]) if diff["unchanged_count"] else 0,
            # Additive fields (2026-07 fix) — see docstring. Old and
            # new names ship alongside the bool so the UI can say
            # WHICH commander changed, not just that one did.
            "commander_changed": commander_changed,
            "local_commanders": local_commanders,
            "remote_commanders": remote_commanders,
        })

    @bp.route("/api/moxfield_format")
    def moxfield_format():
        """Return the deck rendered as a Moxfield-paste-ready string
        (newline-joined card lines). Excludes the [metadata] block."""
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404
        from ..moxfield_push import dck_to_textarea
        try:
            text = dck_to_textarea(path)
        except Exception as exc:  # pragma: no cover
            return jsonify({"error": str(exc)}), 500
        return jsonify({"deck": deck_id, "text": text})

    @bp.route("/api/game_changers")
    def game_changers_route():
        """Return the loaded Game Changers list. Used by the topbar
        Game Changers button + by the dashboard to flag in-deck cards."""
        from ..game_changers import load_game_changers
        try:
            cards = sorted(load_game_changers())
        except Exception as exc:  # pragma: no cover
            return jsonify({"error": str(exc)}), 500
        return jsonify({"cards": cards, "count": len(cards)})

    @bp.route("/api/deck_audit")
    def deck_audit():
        """Audit a deck against bracket-legality + Game Changers.

        Returns:
            {
              "deck_id": "...",
              "bracket": 3,
              "in_deck_game_changers": [...],
              "illegal_cards": [...],   # banned in Commander
              "warnings": [...],
            }
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404
        # Explicitly-provided brackets must be a real Commander bracket
        # (1..5) — silently falling back to the filename on garbage hid
        # client bugs and made the warnings below reflect a bracket the
        # caller never asked about. Absent means "use the filename".
        bracket_raw = request.args.get("bracket")
        if bracket_raw:
            try:
                bracket = int(bracket_raw)
            except ValueError:
                return jsonify({
                    "error": "bracket must be an integer 1..5",
                }), 400
            if bracket not in (1, 2, 3, 4, 5):
                return jsonify({
                    "error": "bracket must be an integer 1..5",
                }), 400
        else:
            bracket = None
        # Default to the deck filename's [B?] suffix.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id)

        from ..game_changers import load_game_changers
        from .. import doctor
        gc_set = load_game_changers()

        # Read deck card names.
        try:
            deck_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        names: list[str] = []
        in_main = False
        in_cmdr = False
        for raw in deck_text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.lower() == "[main]":
                in_main, in_cmdr = True, False
                continue
            if s.lower() == "[commander]":
                in_cmdr, in_main = True, False
                continue
            if s.startswith("[") and s.endswith("]"):
                in_main, in_cmdr = False, False
                continue
            if not (in_main or in_cmdr):
                continue
            import re as _re
            m = _re.match(r"^\d+\s+([^|]+?)(?:\s*\|.*)?$", s)
            if m:
                names.append(m.group(1).strip())

        # Cross-reference Game Changers list. Case-folded membership:
        # the GC list ships canonical casing but deck files are user-
        # or importer-authored ("Rhystic study" happens), and
        # deck_dashboard._count_game_changers already lower-cases —
        # exact-case matching here made the audit count disagree with
        # the dashboard tile for the same deck. Displayed names keep
        # the deck line's original casing.
        gc_lc = {g.lower() for g in gc_set}
        present_gc = sorted({n for n in names if n.lower() in gc_lc})

        # Banned-in-Commander list. We use commander_builder.doctor's
        # check if available, else hand-roll a small core list.
        banned: list[str] = []
        try:
            doctor_banned = getattr(doctor, "BANNED_IN_COMMANDER", None)
            if doctor_banned:
                banned = sorted(set(names) & set(doctor_banned))
        except Exception:
            banned = []
        if not banned:
            # Minimal fallback list of high-profile bans.
            _CORE_BANS = {
                "Ancestral Recall", "Black Lotus", "Mox Ruby", "Mox Pearl",
                "Mox Sapphire", "Mox Emerald", "Mox Jet", "Time Walk",
                "Time Vault", "Library of Alexandria", "Channel",
                "Falling Star", "Shahrazad", "Chaos Orb", "Iona, Shield of Emeria",
                "Limited Resources", "Painter's Servant", "Panoptic Mirror",
                "Primeval Titan", "Recurring Nightmare", "Sundering Titan",
                "Sway of the Stars", "Tempest Efreet", "Time Vault",
                "Tinker", "Trade Secrets", "Upheaval", "Worldfire",
                "Yawgmoth's Bargain", "Coalition Victory",
                "Emrakul, the Aeons Torn", "Erayo, Soratami Ascendant",
                "Hullbreacher", "Sylvan Primordial", "Prophet of Kruphix",
                "Mana Crypt", "Jeweled Lotus", "Dockside Extortionist",
                "Nadu, Winged Wisdom",
            }
            banned = sorted(set(names) & _CORE_BANS)

        warnings = []
        if bracket and bracket <= 3 and present_gc:
            warnings.append(
                f"Bracket {bracket} ({_BRACKET_NAMES.get(bracket, '?')}) "
                f"expects 0 Game Changers; deck contains "
                f"{len(present_gc)}."
            )
        if banned:
            warnings.append(
                f"{len(banned)} card(s) are banned in Commander."
            )

        return jsonify({
            "deck_id": deck_id,
            "bracket": bracket,
            "in_deck_game_changers": present_gc,
            "illegal_cards": banned,
            "warnings": warnings,
        })

    return bp
