"""Small utility / meta routes for the web layer.

Five small routes that don't fit into the deck-edit / sim / audit
/ dashboard groups but are still useful for ops + the topbar:

- ``GET  /``                       (index HTML)
- ``GET  /api/health``             (liveness probe + deck count)
- ``GET  /api/forge_version``      (bundled Forge jar version + age)
- ``GET  /api/correlation_summary`` (forge_py↔Forge correlation log)
- ``POST /api/log_error``          (browser-side error sink)

Built via ``make_meta_blueprint(deck_dir, list_decks,
asset_version)``. ``asset_version`` is a process-local cache-bust
token created at boot so static assets are never served from a
stale browser cache after a restart.

Extracted from ``web/app.py`` as part of the 2026-05-13 blueprint
refactor (tier-3 issue #3.1).
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request


def make_meta_blueprint(
    deck_dir: Path,
    list_decks,
    asset_version: str,
) -> Blueprint:
    """Build a Flask Blueprint for the meta/utility route group.

    ``list_decks`` is the helper still living in ``web/app.py``;
    passed in to keep the blueprint stateless. ``asset_version``
    is the per-process cache-bust token.
    """
    bp = Blueprint("meta", __name__)

    # Browser-side JS error sink path. The browser-side window.onerror
    # / unhandledrejection handlers POST here so silent failures (TDZ
    # ReferenceErrors, async network errors, etc.) land in a server-
    # readable log instead of vanishing in the user's devtools.
    js_error_log = deck_dir.parent.parent / "_js_errors.log"

    @bp.route("/")
    def root():
        return render_template("index.html", asset_version=asset_version)

    @bp.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "deck_dir": str(deck_dir),
            "deck_count": len(list_decks(deck_dir)),
        })

    @bp.route("/api/forge_version")
    def forge_version_route():
        """Surface the bundled Forge jar's version + age so the UI can
        warn when the install is stale enough to misbehave on errata-
        sensitive cards. is_stale=False when age can't be determined
        (no build.txt) — don't alarm on unknowable state.

        ``detect_forge_version`` is resolved lazily through ``web.app``
        (which re-imports it at the top) so test monkeypatches at
        ``commander_builder.web.app.detect_forge_version`` keep
        intercepting calls made from this blueprint. Same lazy-import
        pattern used by ``_advisor_role_helpers`` to preserve test
        patches across the module split.
        """
        from . import app as _app_mod
        info = _app_mod.detect_forge_version()
        return jsonify({
            "version": info.version,
            "jar_path": str(info.jar_path) if info.jar_path else None,
            "build_date": (
                info.build_date.isoformat() if info.build_date else None
            ),
            "age_days": info.age_days,
            "is_stale": info.is_stale,
        })

    @bp.route("/api/correlation_summary")
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

    @bp.route("/api/log_error", methods=["POST"])
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
            js_error_log.parent.mkdir(parents=True, exist_ok=True)
            with js_error_log.open("a", encoding="utf-8") as f:
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

    return bp
