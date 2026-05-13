"""FP-006 Flask scaffold — serves the deck-dashboard JSON feed and a
placeholder root page.

Routes:
    GET  /                          -> placeholder HTML (no design yet)
    GET  /api/health                -> {"status": "ok"}
    GET  /api/decks                 -> {"decks": [{"id", "name", "path"}, ...]}
    GET  /api/dashboard?deck=<id>   -> DashboardData JSON for that deck
    GET  /api/dashboard?path=<p>    -> DashboardData JSON for an explicit path
    GET  /api/iterations            -> recent iterations across all decks
    GET  /api/iterations?deck=<id>  -> iteration history for one deck

Notes:
- The deck index is built from a single ``deck_dir`` configured at
  app-create time. Pass ``deck_dir=Path(...)`` to ``create_app`` or set
  the ``COMMANDER_BUILDER_DECK_DIR`` env var. Falls back to CWD/decks.
- The Flask import is deferred so this module is harmless to import
  when the ``[web]`` extra is missing — only ``create_app`` raises.
- All paths are validated against ``deck_dir`` to prevent traversal.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ..deck_dashboard import build_dashboard
from ..forge_runner import detect_forge_version
from ..knowledge_log import (
    DEFAULT_DB_PATH as _DEFAULT_KLOG_DB,
    Iteration,
    get_iteration,
    iterations_for_deck,
    recent_iterations,
    record_iteration,
    stats_summary,
)


def _cleanup_stale_staged_files(
    deck_dir: Path, age_threshold_sec: int = 60,
) -> int:
    """Sweep stale ``*_proposed_<ts>.dck`` / ``*_converted_<ts>.dck``
    files left behind by interrupted propose-swap runs (Ctrl-C, server
    crash, network failure).

    Only files older than ``age_threshold_sec`` are removed — protects
    against deleting files an in-flight Forge process is still
    reading. Returns the number of files deleted. Best-effort: never
    raises; logs failures for the caller to surface if it cares.
    """
    import re as _re
    import time as _time
    if not deck_dir.exists():
        return 0
    pattern = _re.compile(r"_(proposed|converted)_\d{8}_\d{6}\.dck$")
    now = _time.time()
    deleted = 0
    # Sweep the commander folder + the parallel constructed folder
    # used by 1v1 mode.
    candidates = []
    for sub in (deck_dir, deck_dir.parent / "constructed"):
        if sub.exists() and sub.is_dir():
            candidates.extend(sub.glob("*.dck"))
    for p in candidates:
        if not pattern.search(p.name):
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < age_threshold_sec:
            continue
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def _list_decks(deck_dir: Path, user_only: bool = True) -> list[dict]:
    """Enumerate ``.dck`` files under ``deck_dir`` (non-recursive).

    By default returns only ``[USER] *.dck`` files — those are the
    decks under active iteration. Set ``user_only=False`` to also
    list filler / pool decks (used by curation commands).

    Always hides ``*_proposed_<timestamp>.dck`` files. Those are
    transient working copies the propose-swap A/B-sim flow stages
    while running Forge; they shouldn't pollute the sidebar.
    """
    if not deck_dir.exists() or not deck_dir.is_dir():
        return []
    out: list[dict] = []
    import re as _re
    for p in sorted(deck_dir.glob("*.dck")):
        if user_only and not p.stem.startswith("[USER]"):
            continue
        # Skip transient propose-swap working copies regardless of mode.
        if _re.search(r"_(proposed|converted)_\d{8}_\d{6}$", p.stem):
            continue
        display = _re.sub(r"^\[USER\]\s*", "", p.stem)
        out.append({
            "id": p.stem,
            "name": display,
            "path": str(p),
        })
    return out


def _resolve_deck_path(
    deck_dir: Path, deck_id: Optional[str], explicit_path: Optional[str],
) -> Optional[Path]:
    """Resolve a deck identifier or explicit path to a real file.

    Both forms are validated against ``deck_dir`` — explicit paths must
    be inside ``deck_dir`` after resolution, otherwise None is returned.
    """
    if deck_id:
        candidate = (deck_dir / f"{deck_id}.dck").resolve()
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    if explicit_path:
        candidate = Path(explicit_path).resolve()
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    return None


def create_app(
    deck_dir: Optional[Path] = None,
    knowledge_db: Optional[Path] = None,
):
    """Build the Flask app. Imports flask lazily so the rest of
    commander_builder works without the ``[web]`` extra installed."""
    try:
        from flask import Flask, jsonify, render_template, request
    except ImportError as exc:
        raise RuntimeError(
            "flask is required for the web scaffold. "
            "Install with: pip install commander-builder[web]"
        ) from exc

    if deck_dir is None:
        env_dir = os.environ.get("COMMANDER_BUILDER_DECK_DIR")
        if env_dir:
            deck_dir = Path(env_dir)
        else:
            # Canonical location every other module reads/writes from.
            # Forge's `sim` mode requires decks live under
            # `userdata/decks/commander/`; pointing the web app
            # elsewhere split the world (import would land in
            # CWD/decks/ but compare/audit would look here).
            from ..forge_runner import VENDOR_FORGE
            deck_dir = VENDOR_FORGE / "userdata" / "decks" / "commander"
    deck_dir = deck_dir.resolve()

    if knowledge_db is None:
        env_db = os.environ.get("COMMANDER_BUILDER_KNOWLEDGE_DB")
        knowledge_db = Path(env_db) if env_db else _DEFAULT_KLOG_DB
    knowledge_db = Path(knowledge_db)

    # Sweep transient propose-swap staging files left over from
    # interrupted prior runs. Stale-by-definition: their filenames
    # carry timestamps so a fresh run never collides with live work.
    try:
        _stale_swept = _cleanup_stale_staged_files(deck_dir)
        if _stale_swept:
            print(
                f"[startup] swept {_stale_swept} stale staging file(s)",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass

    # Forge jar version + age check. New MTG sets ship every 4-6 weeks;
    # errata-sensitive cards (Sephiroth, Vivi) silently misbehave on
    # old jars. Surface the version at boot so the operator can decide
    # whether to grab a fresh build from github.com/Card-Forge/forge/
    # releases. Best-effort: never raises.
    try:
        _forge_info = detect_forge_version()
        if _forge_info.version:
            age_str = (
                f"{_forge_info.age_days}d old"
                if _forge_info.age_days is not None
                else "age unknown"
            )
            if _forge_info.is_stale:
                print(
                    f"[startup] WARN: Forge jar {_forge_info.version} is "
                    f"{age_str} — consider updating from "
                    f"github.com/Card-Forge/forge/releases",
                    flush=True,
                )
            else:
                print(
                    f"[startup] Forge jar {_forge_info.version} ({age_str})",
                    flush=True,
                )
        else:
            print(
                "[startup] WARN: no Forge jar found in vendor/forge/",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass

    app = Flask(__name__)
    app.config["DECK_DIR"] = deck_dir
    app.config["KNOWLEDGE_DB"] = knowledge_db

    # Cache-buster: a fresh token per process boot so static assets
    # are never served from a stale browser cache after a restart.
    # Without this, app.js / app.css edits ship to GitHub but the
    # browser keeps the old copy and the user sees stale UI behavior
    # (e.g. a missing Mode radio while the template clearly has it).
    import secrets as _secrets
    _ASSET_VERSION = _secrets.token_hex(4)

    @app.route("/")
    def root():
        return render_template("index.html", asset_version=_ASSET_VERSION)

    @app.route("/api/correlation_summary")
    def correlation_summary_route():
        """Read the forge_py↔Forge correlation log and return summary
        stats. UI can show "forge_py agrees X% of the time across N
        runs" so the user knows the Track-2 dataset is growing."""
        from ..forge_py_correlation import correlation_summary
        log_path = deck_dir.parent.parent / "_forge_py_correlation.csv"
        try:
            stats = correlation_summary(log_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({
                "error": "could not read correlation log",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 500
        stats["log_path"] = str(log_path)
        stats["enabled"] = bool(
            os.environ.get(
                "COMMANDER_BUILDER_CORRELATE_FORGE_PY", "",
            ).strip().lower() in ("1", "true", "yes"),
        )
        return jsonify(stats)

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "deck_dir": str(deck_dir),
            "deck_count": len(_list_decks(deck_dir)),
        })

    @app.route("/api/forge_version")
    def forge_version_route():
        """Surface the bundled Forge jar's version + age so the UI can
        warn when the install is stale enough to misbehave on errata-
        sensitive cards. is_stale=False when age can't be determined
        (no build.txt) — don't alarm on unknowable state."""
        info = detect_forge_version()
        return jsonify({
            "version": info.version,
            "jar_path": str(info.jar_path) if info.jar_path else None,
            "build_date": (
                info.build_date.isoformat() if info.build_date else None
            ),
            "age_days": info.age_days,
            "is_stale": info.is_stale,
        })

    # JS error collector. The browser-side window.onerror /
    # unhandledrejection handlers POST here so silent failures
    # (TDZ ReferenceErrors, async network errors, etc.) land
    # in a server-readable log instead of vanishing in the user's
    # devtools. Plain text — easy to grep, easy to copy into chat.
    _JS_ERROR_LOG = deck_dir.parent.parent / "_js_errors.log"

    @app.route("/api/log_error", methods=["POST"])
    def log_error():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "expected JSON body"}), 400
        msg = (payload.get("message") or "").strip()
        if not msg:
            return jsonify({"error": "message is required"}), 400
        # Cap fields so a runaway browser doesn't bloat the log.
        msg = msg[:2000]
        url = (payload.get("url") or "")[:512]
        stack = (payload.get("stack") or "")[:4000]
        ua = (payload.get("user_agent") or "")[:256]
        kind = (payload.get("kind") or "error")[:40]
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.now(_tz.utc).isoformat()
        # Append-only; never read from this endpoint.
        try:
            _JS_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _JS_ERROR_LOG.open("a", encoding="utf-8") as f:
                f.write(f"--- {ts} [{kind}] {url}\n")
                f.write(f"UA: {ua}\n")
                f.write(f"MSG: {msg}\n")
                if stack:
                    f.write(f"STACK:\n{stack}\n")
                f.write("\n")
        except OSError as exc:
            return jsonify({
                "error": "could not write log",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 500
        # Hand the user a short reference token they can copy into chat.
        # Format: ts + first 4 hex chars of message hash. Not crypto,
        # just a "this is the one I'm complaining about" handle.
        import hashlib as _hashlib
        ref = ts.replace(":", "").replace("-", "")[:14] + "-" + (
            _hashlib.sha1((msg + stack).encode("utf-8")).hexdigest()[:4]
        )
        return jsonify({"ok": True, "ref": ref})

    @app.route("/api/decks")
    def decks():
        # Default: only [USER] decks. Pass ?all=1 to include filler/pool.
        all_flag = request.args.get("all", "").lower() in ("1", "true", "yes")
        return jsonify({
            "decks": _list_decks(deck_dir, user_only=not all_flag),
        })

    @app.route("/api/dashboard")
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
                app.logger.warning("advise failed: %s", exc)

        data = build_dashboard(path, bracket=bracket, suggested=suggested)
        return jsonify(data.to_dict())

    @app.route("/api/audit")
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
        byo_key = request.headers.get("X-Anthropic-API-Key", "").strip()
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
        # Trim the rendered payloads to exactly the swaps that were
        # actually applied to the deck text. _apply_swaps_to_dck
        # balances adds==cuts to keep deck size legal; without this
        # filter, the UI would say "5 added 8 removed" while only 5+5
        # of those changes actually landed.
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
            }
            for rec in report.recommendations
            if rec.action == "add" and rec.card.lower() in applied_add_set
        ]
        removed_payload = [
            {
                "card": rec.card,
                "rationale": rec.reason or "",
                "name_known": getattr(rec, "name_known", True),
            }
            for rec in report.recommendations
            if rec.action == "cut" and rec.card.lower() in applied_cut_set
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
        if actual_source != requested:
            # advise() silently falls back to heuristic when the
            # requested backend can't run (no Claude API key, no
            # bracket-peer references found, network blip). Tell the
            # UI exactly why so the user can fix it or accept the
            # degraded source instead of guessing.
            if fallback_reason:
                warning = (
                    f"{requested} backend fell back to EDHREC heuristic. "
                    f"Reason: {fallback_reason}"
                )
            elif (requested == "claude"
                  and not byo_key
                  and not os.environ.get("ANTHROPIC_API_KEY")):
                warning = (
                    "Claude analyst was requested but no API key was "
                    "provided. Click 'Set API key' (sk-…) and try again."
                )
            else:
                warning = (
                    f"{requested} backend was requested but unavailable — "
                    "falling back to EDHREC heuristic."
                )
        return jsonify({
            "deck": deck_id,
            "bracket": bracket,
            "proposed_text": proposed_text,
            "added": added_payload,
            "removed": removed_payload,
            "kept_count": kept,
            "main_count": kept + len(added_payload) + padded_count,
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
            # Adds the saturation guard dropped because the deck
            # already has enough cards in that role bucket. Each
            # entry: {card, role, deck_count, threshold}. Lets the UI
            # show "skipped 3 ramp adds — you already have 13" so a
            # short list isn't mistaken for the advisor giving up.
            "skipped_for_saturation": list(
                getattr(report, "skipped_for_saturation", []) or []
            ),
        })

    @app.route("/api/advise")
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

    @app.route("/api/iterations")
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

    @app.route("/api/deck_text", methods=["GET", "PUT", "DELETE"])
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
            try:
                payload = request.get_json(force=True) or {}
            except Exception:
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

    @app.route("/api/import_deck", methods=["POST"])
    def import_deck():
        """Create a new .dck under deck_dir from either a Moxfield URL
        or a paste of the deck text.

        Body: ``{"name": "<display>", "moxfield_url": "<url>"}``  OR
              ``{"name": "<display>", "paste_text": "<.dck or moxfield-format>"}``

        Filename is derived from ``name`` with a ``[USER]`` prefix and
        ``[B?]`` suffix so the deck shows up in the user-only sidebar.
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "expected JSON body"}), 400

        name = (payload.get("name") or "").strip()
        url = (payload.get("moxfield_url") or "").strip()
        paste = (payload.get("paste_text") or "").strip()
        try:
            bracket = int(payload.get("bracket") or 3)
        except (TypeError, ValueError):
            bracket = 3

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
            # Paste path. Accept both .dck format (with [Main] sections)
            # and the plain Moxfield bulk-paste line list.
            deck_text_out = _normalize_pasted_deck(paste)
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

    @app.route("/api/deck_source", methods=["GET", "PUT"])
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
        text = path.read_text(encoding="utf-8")

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
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
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

    @app.route("/api/verify_against_source")
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
            }
        """
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({"error": "deck not found"}), 404
        text = path.read_text(encoding="utf-8")
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
        return jsonify({
            "deck": deck_id,
            "source_url": f"https://moxfield.com/decks/{mox_id}",
            "in_local_only": diff["removed"],
            "in_remote_only": diff["added"],
            "matched": int(diff["unchanged_count"][0]) if diff["unchanged_count"] else 0,
        })

    @app.route("/api/moxfield_format")
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

    @app.route("/api/game_changers")
    def game_changers_route():
        """Return the loaded Game Changers list. Used by the topbar
        Game Changers button + by the dashboard to flag in-deck cards."""
        from ..game_changers import load_game_changers
        try:
            cards = sorted(load_game_changers())
        except Exception as exc:  # pragma: no cover
            return jsonify({"error": str(exc)}), 500
        return jsonify({"cards": cards, "count": len(cards)})

    @app.route("/api/deck_audit")
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
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            bracket = None
        # Default to the deck filename's [B?] suffix.
        if bracket is None:
            bracket = _bracket_from_filename(deck_id)

        from ..game_changers import load_game_changers
        from .. import doctor
        gc_set = load_game_changers()

        # Read deck card names.
        names: list[str] = []
        in_main = False
        in_cmdr = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.lower() == "[main]":
                in_main, in_cmdr = True, False; continue
            if s.lower() == "[commander]":
                in_cmdr, in_main = True, False; continue
            if s.startswith("[") and s.endswith("]"):
                in_main, in_cmdr = False, False; continue
            if not (in_main or in_cmdr):
                continue
            import re as _re
            m = _re.match(r"^\d+\s+([^|]+?)(?:\s*\|.*)?$", s)
            if m:
                names.append(m.group(1).strip())

        # Cross-reference Game Changers list.
        present_gc = sorted({n for n in names if n in gc_set})

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


    @app.route("/api/propose_swap", methods=["POST"])
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
        try:
            from flask import abort
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "expected JSON body"}), 400

        deck_id = payload.get("deck")
        new_text = payload.get("new_text") or ""
        try:
            games = int(payload.get("games", 5))
        except (TypeError, ValueError):
            return jsonify({"error": "games must be int"}), 400
        if games not in (5, 10, 20):
            return jsonify({
                "error": "games must be one of 5, 10, 20",
            }), 400
        try:
            bracket = int(payload.get("bracket", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "bracket must be int"}), 400
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
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
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
        new_path = stage_dir / f"{bare_stem}_proposed_{ts}.dck"
        # Rewrite the [metadata] Name= field so Forge displays this
        # deck distinctly from the original. Without this both decks
        # report 'Name=Hakbal of the Surging Soul' in Forge's Match
        # Result lines and log_parser can't attribute wins to either
        # side — every game looks like a tie regardless of who won.
        import re as _re_meta
        staged_name = f"{bare_stem}_proposed_{ts}"
        if _re_meta.search(r"^Name=.+$", new_text, flags=_re_meta.MULTILINE):
            new_text_staged = _re_meta.sub(
                r"^Name=.+$", f"Name={staged_name}", new_text,
                count=1, flags=_re_meta.MULTILINE,
            )
        else:
            # No metadata Name line — synthesize one at the top.
            if "[metadata]" in new_text.lower():
                new_text_staged = _re_meta.sub(
                    r"(\[metadata\][^\n]*\n)", rf"\1Name={staged_name}\n",
                    new_text, count=1, flags=_re_meta.IGNORECASE,
                )
            else:
                new_text_staged = (
                    f"[metadata]\nName={staged_name}\n\n" + new_text
                )

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
        # a sibling _converted_<timestamp>.dck holding the converted
        # text so we don't mutate the user's actual deck file.
        old_converted_path: Optional[Path] = None
        old_for_compare = old_path.name
        if mode == "1v1":
            old_text = old_path.read_text(encoding="utf-8")
            # Ensure the old deck's metadata Name= is also distinct
            # so log_parser can split wins between old + new.
            old_staged_name = f"{bare_stem}_converted_{ts}"
            if _re_meta.search(r"^Name=.+$", old_text, flags=_re_meta.MULTILINE):
                old_text = _re_meta.sub(
                    r"^Name=.+$", f"Name={old_staged_name}", old_text,
                    count=1, flags=_re_meta.MULTILINE,
                )
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
            except OSError:
                pass
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
                except OSError:
                    pass
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
                app.logger.warning(
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
            except OSError:
                pass

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
            # Sprint 1C telemetry: per-pod intra-pod abort summary so
            # the UI can show "Pod 2 stopped at game 3/5 (decisive)".
            "pod_summaries": [
                {
                    "pod_index": p.get("pod_index", i + 1),
                    "intra_pod_aborted": bool(p.get("intra_pod_aborted")),
                    "games_actually_played": int(
                        p.get("games_actually_played") or 0
                    ),
                    "duration_sec": p.get("duration_sec", 0),
                }
                for i, p in enumerate(report.pods)
            ],
        })

    @app.route("/api/save_iteration", methods=["POST"])
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
                "verdict": "kept" | "reverted" | "neutral" | "pending",
                "verdict_notes": "..." (optional),
                "deck_snapshot": "<.dck text>" (optional),
                "parent_id": int (optional)
            }

        Returns ``{"id": <new row id>, "stats": <stats_summary>}`` so the
        UI can show "Saved iteration #N — knowledge_log now has X rows."
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
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
        if verdict not in ("kept", "reverted", "neutral", "pending"):
            return jsonify({
                "error": "verdict must be one of kept, reverted, neutral, pending",
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
        win_rate_old = None
        win_rate_new = None
        margin = None
        if isinstance(sim_report, dict):
            try:
                old_g = int(sim_report.get("old_games") or 0)
                new_g = int(sim_report.get("new_games") or 0)
                old_w = int(sim_report.get("old_wins") or 0)
                new_w = int(sim_report.get("new_wins") or 0)
                if old_g > 0:
                    win_rate_old = old_w / old_g
                if new_g > 0:
                    win_rate_new = new_w / new_g
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

    @app.route("/api/iteration/<int:iteration_id>")
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

    @app.route("/api/compare/<int:old_id>/<int:new_id>")
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

    @app.route("/api/iteration/<int:iteration_id>/snapshot")
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
        from flask import Response
        return Response(it.deck_snapshot, mimetype="text/plain")

    return app


_BRACKET_NAMES = {
    1: "Exhibition", 2: "Core", 3: "Upgraded", 4: "Optimized", 5: "cEDH",
}


def _bracket_from_filename(deck_id: str | None) -> int | None:
    """Parse the ``[B<n>]`` suffix the user encodes in deck filenames.

    Returns the bracket integer (1..5) or None if the suffix is missing
    or unparseable. The filename is the user's declared/intended
    bracket; it should override the heuristic guess unless the request
    explicitly passes a different bracket.
    """
    if not deck_id:
        return None
    import re as _re
    m = _re.search(r"\[B(\d)\](?:\.dck)?\s*$", deck_id)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 5 else None


def _normalize_pasted_deck(text: str) -> str:
    """Accept either a Forge-format .dck blob or a Moxfield bulk-paste
    line list and return a valid .dck. Bulk-paste shape is one line
    per card: ``<qty> <Name>`` with no `[Main]` header. We detect that
    by looking for any `[section]` markers; if none, wrap the lines
    in a `[Main]` section.
    """
    text = text.strip()
    if not text:
        return ""
    # If the paste already has section headers, trust the user.
    has_section = any(
        line.strip().startswith("[") and line.strip().endswith("]")
        for line in text.splitlines()
    )
    if has_section:
        return text + "\n"
    # Otherwise wrap in [Main]. Filter trivial header lines like
    # "Mainboard (99)" that Moxfield's UI sometimes includes.
    body_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Skip obvious headers (e.g. "Commander (1)", "Mainboard (99)").
        if s.lower().startswith(("mainboard", "commander", "sideboard",
                                 "considering")):
            continue
        body_lines.append(s)
    return "[Main]\n" + "\n".join(body_lines) + "\n"


def _match_pct_from_evidence(evidence: dict | None) -> int:
    """Mirror deck_dashboard.match_score combination so audit output
    uses the same 1..100 scale the suggestion panel renders.

    Bracket-peers recs (source="bracket_peers") only set
    ``in_n_references`` / ``total_references`` rather than the EDHREC
    inclusion%/synergy% pair. When those are present we compute
    ``100 * in_n_references / total_references`` — a card in 5/5
    references shows 100, 3/5 shows 60. This keeps the UI's match-pct
    pill meaningful across all sources without bracket_peers needing
    to fabricate EDHREC-shaped fields.
    """
    if not evidence:
        return 0
    # Prefer reference-frequency math when bracket_peers fields are set.
    total = evidence.get("total_references")
    in_n = evidence.get("in_n_references")
    if isinstance(total, int) and total > 0 and isinstance(in_n, int):
        return max(1, min(100, round(100 * in_n / total)))
    inclusion = float(evidence.get("inclusion_pct") or 0)
    synergy = min(float(evidence.get("synergy_pct") or 0), 20.0)
    raw = inclusion + synergy
    return max(1, min(100, round(raw)))


def _to_constructed_format(text: str) -> str:
    """Convert a Forge commander .dck to a 1v1-constructed-loadable
    .dck.

    Forge's `sim -f constructed` mode silently produces zero games
    when the deck has a ``[Commander]`` section — the format flag
    doesn't match the deck shape, so Forge loads the file but never
    actually starts a match. The fix is to:

    1. Move the commander line into ``[Main]`` so the deck is just
       a single 100-card stack of cards Forge can shuffle.
    2. Drop the ``[Commander]`` section header.
    3. Stamp ``Deck Type=constructed`` into ``[metadata]`` so Forge's
       deck-type detector picks the right rule set.

    This mirrors what forge_py's correlate_with_forge.py harness has
    been doing for the round-robin study; the propose-swap web endpoint
    needs the same conversion before handing decks to Forge.
    """
    import re as _re
    out: list[str] = []
    in_cmdr = False
    in_meta = False
    cmdr_lines: list[str] = []
    seen_metadata = False
    deck_type_set = False

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            out.append(raw)
            continue
        if s.lower() == "[commander]":
            in_cmdr = True
            in_meta = False
            continue  # drop the header
        if s.lower() == "[metadata]":
            in_meta = True
            in_cmdr = False
            seen_metadata = True
            out.append(raw)
            continue
        if s.startswith("[") and s.endswith("]"):
            in_cmdr = False
            in_meta = False
            out.append(raw)
            continue
        if in_cmdr:
            cmdr_lines.append(s)
            continue
        if in_meta and s.lower().startswith("deck type="):
            out.append("Deck Type=constructed")
            deck_type_set = True
            continue
        out.append(raw)

    new_text = "\n".join(out)
    if not seen_metadata:
        new_text = "[metadata]\nDeck Type=constructed\n\n" + new_text
    elif not deck_type_set:
        # Insert under existing metadata block.
        new_text = _re.sub(
            r"(\[metadata\][^\n]*\n)",
            r"\1Deck Type=constructed\n",
            new_text, count=1, flags=_re.IGNORECASE,
        )
    # Append commander lines to [Main]. If [Main] doesn't exist, add it.
    if cmdr_lines:
        cmdr_block = "\n".join(cmdr_lines) + "\n"
        if _re.search(r"^\[Main\]\s*$", new_text, _re.MULTILINE | _re.IGNORECASE):
            # Insert just after the [Main] header. Use a callable
            # replacement so card-line content (which starts with
            # digits like "1 Hakbal") doesn't get parsed as numeric
            # backreferences (\1 → \11 collision).
            new_text = _re.sub(
                r"(\[Main\][^\n]*\n)",
                lambda m: m.group(0) + cmdr_block,
                new_text, count=1, flags=_re.IGNORECASE,
            )
        else:
            new_text += "\n[Main]\n" + cmdr_block
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text


def _format_added_line(name: str) -> str:
    """Render a `1 <Name>|<SET>|<CN>` line for an added card.

    Forge's deck loader can be strict about ambiguous name-only
    lookups (alternate art, reprints across many sets, special
    characters like //). We resolve each appended card to its
    current Scryfall printing so the proposed deck loads cleanly.

    The shared ``oracle_snapshots`` cache stores forge_py-projected
    snapshots that don't carry ``set`` / ``collector_number`` fields
    (those are stripped to keep payload size small). When the cached
    snapshot lacks them we fall through to a cache-bypassed Scryfall
    fetch, which returns the full payload and re-caches it. Plain
    ``1 <name>`` is the final fallback when Scryfall is unreachable.
    """
    try:
        from ..scryfall_client import lookup_card
        data = lookup_card(name) or {}
        set_code = (data.get("set") or "").upper()
        cn = data.get("collector_number") or ""
        if not (set_code and cn):
            # Cached snapshot was the projected shape — fetch fresh.
            data = lookup_card(name, cache=False) or {}
            set_code = (data.get("set") or "").upper()
            cn = data.get("collector_number") or ""
    except Exception:
        return f"1 {name}"
    if set_code and cn:
        return f"1 {name}|{set_code}|{cn}"
    return f"1 {name}"


_BASIC_LANDS = ("Forest", "Island", "Plains", "Swamp", "Mountain", "Wastes")


def _pad_main_to_99(text: str, current_main: int) -> tuple[str, int, dict[str, int]]:
    """Top up the [Main] section with basic lands until it hits 99.

    The user's source decks sometimes ship short of legal Commander
    size (e.g. the Goblin deck is 71 mainboard). The advisor's adds==
    cuts balance preserves any deficit, so the proposed deck inherits
    it and Forge refuses to load it. Pad with basics matching the
    distribution already present in the deck — preserves color balance
    without us needing to round-trip Scryfall for the commander's color
    identity.

    Returns ``(padded_text, padding_added, breakdown)`` where breakdown
    is ``{basic_name: count_added}``. If the deck is already at or above
    99 mainboard, returns the input text and an empty breakdown.
    """
    import re as _re
    if current_main >= 99:
        return text, 0, {}
    deficit = 99 - current_main

    # Count basics currently in [Main] so we mirror the user's distribution.
    counts: dict[str, int] = {b: 0 for b in _BASIC_LANDS}
    in_main = False
    qty_name_re = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            continue
        if not in_main:
            continue
        m = qty_name_re.match(stripped)
        if not m:
            continue
        name = m.group(2).strip()
        if name in counts:
            try:
                counts[name] += int(m.group(1))
            except (TypeError, ValueError):
                counts[name] += 1

    basics_present = {b: c for b, c in counts.items() if c > 0}
    # No basics? Fall back to Wastes (colorless, legal in any color identity).
    # Better than guessing colors blind.
    if not basics_present:
        basics_present = {"Wastes": 1}

    total = sum(basics_present.values())
    pad: dict[str, int] = {}
    distributed = 0
    # Largest share first (sorted descending) so floor-rounding leftovers
    # gravitate to the dominant color.
    for b, c in sorted(basics_present.items(), key=lambda kv: -kv[1]):
        share = (c * deficit) // total
        if share > 0:
            pad[b] = share
            distributed += share
    leftover = deficit - distributed
    if leftover > 0:
        top = max(basics_present, key=lambda b: basics_present[b])
        pad[top] = pad.get(top, 0) + leftover

    # Render new lines and splice them at the end of [Main].
    pad_lines = [f"{n} {b}" for b, n in pad.items() if n > 0]
    out_lines: list[str] = []
    in_main = False
    inserted = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_main and not inserted:
                out_lines.extend(pad_lines)
                inserted = True
            in_main = stripped.lower() == "[main]"
        out_lines.append(raw)
    if in_main and not inserted:
        out_lines.extend(pad_lines)

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, deficit, pad


def _apply_swaps_to_dck(
    original_text: str, recommendations,
) -> tuple[str, list[str], list[str], int]:
    """Apply add / cut recommendations to a .dck blob.

    Returns ``(new_text, added_card_names, removed_card_names,
    kept_count)``. Quantity-1 lines are added by default.

    Handling:
    - The [Commander] section is preserved as-is (audits never cut commanders).
    - The [Main] section is rebuilt: drop any line whose card name
      matches a `cut` recommendation; append `1 <card>` for each `add`.
    - Other sections (sideboard, considering, metadata) are preserved.

    **Adds and cuts are balanced** — Commander needs exactly 99 main +
    1 commander. The advisor's heuristic produces M adds and N cuts
    independently and they're often unequal, leaving an illegal deck
    that Forge refuses to load. We trim whichever list is longer so
    both have ``min(M, N)`` entries (priority: keep adds, since they
    came from EDHREC's top-cards rank order; drop the *bottom* of the
    cuts list, since those are the lowest-confidence "doesn't appear
    in EDHREC top/synergy" guesses).

    Card names are matched case-insensitively against the leading
    ``<qty> <Name>[|<SET>|<CN>]`` pattern.
    """
    import re as _re
    add_names = [r.card for r in recommendations if r.action == "add"]
    cuts = [r.card for r in recommendations if r.action == "cut"]
    # Balance to keep Commander deck size legal.
    n = min(len(add_names), len(cuts))
    add_names = add_names[:n]
    cuts = cuts[:n]
    cut_set = {c.lower() for c in cuts}

    out_lines: list[str] = []
    in_main = False
    main_kept = 0
    in_metadata = False
    line_pattern = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")

    for raw in original_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            out_lines.append(raw)
            continue
        # Section header tracking.
        if stripped.startswith("[") and stripped.endswith("]"):
            # If we're leaving [Main], this is where we append the new
            # cards (so they're inside the section, not after it).
            if in_main:
                for name in add_names:
                    out_lines.append(_format_added_line(name))
            in_main = stripped.lower() == "[main]"
            in_metadata = stripped.lower() == "[metadata]"
            out_lines.append(raw)
            continue

        if in_main:
            m = line_pattern.match(stripped)
            if m:
                name = m.group(2).strip().lower()
                if name in cut_set:
                    continue  # drop this line
                # Sum the quantity prefix, not the line count — `5 Forest`
                # is one line but five cards. Counting lines made the UI
                # report '83 mainboard' on a deck Forge actually loaded
                # as the legal 99.
                try:
                    main_kept += int(m.group(1))
                except (TypeError, ValueError):
                    main_kept += 1
            out_lines.append(raw)
            continue

        # Non-main section content passes through.
        out_lines.append(raw)

    # If [Main] was the last section, append-on-exit didn't fire above.
    # Detect by checking whether all add_names already landed.
    if in_main:
        for name in add_names:
            out_lines.append(_format_added_line(name))

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, add_names, cuts, main_kept


def _build_suggested_adds(deck_path: Path, bracket: int) -> list[dict]:
    """Project ``improvement_advisor.advise()`` recommendations into the
    shape ``deck_dashboard.build_dashboard`` expects for
    ``suggested``::

        [{"card": str, "inclusion_pct": float, "synergy_pct": float,
          "rationale": str, "price_usd": Optional[float]}, ...]

    Only `add` actions are forwarded — the dashboard's "suggested
    adds" panel is for cards to consider including, not cuts.
    Pulled out as a helper so both `/api/dashboard?advise=1` and
    `/api/advise` reuse the same projection.
    """
    from ..improvement_advisor import advise
    report = advise(deck_path, bracket=bracket)
    out: list[dict] = []
    for rec in report.recommendations:
        if rec.action != "add":
            continue
        ev = rec.evidence or {}
        out.append({
            "card": rec.card,
            "inclusion_pct": float(ev.get("inclusion_pct") or 0),
            "synergy_pct": float(ev.get("synergy_pct") or 0),
            "rationale": rec.reason or "",
            "price_usd": ev.get("price_usd"),
        })
    return out


def _iteration_to_dict(it) -> dict:
    """JSON-friendly projection of an Iteration. Drops the deck_snapshot
    blob to keep payloads small — callers can re-request the full row
    via /api/iteration/<id> if we add it later."""
    return {
        "id": it.id,
        "deck_id": it.deck_id,
        "deck_name": it.deck_name,
        "bracket": it.bracket,
        "parent_id": it.parent_id,
        "audit_version": it.audit_version,
        "audit_manifest": it.audit_manifest,
        "verdict": it.verdict,
        "verdict_notes": it.verdict_notes,
        "win_rate_old": it.win_rate_old,
        "win_rate_new": it.win_rate_new,
        "margin": it.margin,
        "created_at": it.created_at,
    }


def main() -> int:
    """Entry point: ``python -m commander_builder.web``."""
    import argparse
    ap = argparse.ArgumentParser(prog="commander-builder-web")
    ap.add_argument(
        "--deck-dir", type=Path, default=None,
        help="Directory containing .dck files (default: $COMMANDER_BUILDER_DECK_DIR or ./decks)",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    app = create_app(deck_dir=args.deck_dir)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
