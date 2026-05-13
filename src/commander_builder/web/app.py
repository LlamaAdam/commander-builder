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

import json
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
    pricing_series_for_deck,
    recent_iterations,
    record_iteration,
    stats_summary,
    verdict_breakdown_for_deck,
)

# Pure helpers extracted to ``_helpers.py`` as part of the
# 2026-05-13 blueprint refactor (tier-3 issue #3.1). Re-exported
# here so tests + external callers that imported them via
# ``commander_builder.web.app`` keep working unchanged.
from ._helpers import (  # noqa: F401
    _apply_swaps_to_dck,
    _bracket_from_filename,
    _build_suggested_adds,
    _format_added_line,
    _iteration_to_dict,
    _match_pct_from_evidence,
    _normalize_pasted_deck,
    _pad_main_to_99,
    _to_constructed_format,
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

    # Register modular route blueprints. As of the 2026-05-13 tier-3
    # blueprint refactor (issue #3.1), route groups live in
    # ``routes_<group>.py`` modules and are wired in here. The
    # remaining inline routes are being migrated incrementally.
    from .routes_audit import make_audit_blueprint
    from .routes_sim import make_sim_blueprint
    app.register_blueprint(
        make_audit_blueprint(deck_dir, _resolve_deck_path),
    )
    app.register_blueprint(
        make_sim_blueprint(deck_dir, knowledge_db, _resolve_deck_path),
    )

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

    @app.route("/api/pricing_series")
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

    @app.route("/api/verdict_breakdown")
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



    return app


_BRACKET_NAMES = {
    1: "Exhibition", 2: "Core", 3: "Upgraded", 4: "Optimized", 5: "cEDH",
}


# NOTE — the pure helpers that used to live here (everything from
# ``_bracket_from_filename`` through ``_iteration_to_dict``, plus the
# ``_BASIC_LANDS`` constant) were extracted to ``web/_helpers.py`` as
# part of the 2026-05-13 blueprint refactor (tier-3 issue #3.1).
# They're re-exported above so tests and external callers that
# import them via ``commander_builder.web.app`` keep working.



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
