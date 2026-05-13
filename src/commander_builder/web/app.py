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
    from .routes_decks import make_decks_blueprint
    from .routes_sim import make_sim_blueprint
    app.register_blueprint(
        make_audit_blueprint(deck_dir, _resolve_deck_path),
    )
    app.register_blueprint(
        make_decks_blueprint(deck_dir, _resolve_deck_path),
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


    return app


# NOTE — the pure helpers that used to live here (everything from
# ``_bracket_from_filename`` through ``_iteration_to_dict``, plus the
# ``_BASIC_LANDS`` constant) were extracted to ``web/_helpers.py`` as
# part of the 2026-05-13 blueprint refactor (tier-3 issue #3.1).
# They're re-exported above so tests and external callers that
# import them via ``commander_builder.web.app`` keep working.
#
# The route handlers themselves now live in the per-group blueprint
# modules: ``routes_audit.py``, ``routes_sim.py``, ``routes_decks.py``.
# ``_BRACKET_NAMES`` (used only by deck_audit) moved with that route
# group into ``routes_decks.py``.



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
