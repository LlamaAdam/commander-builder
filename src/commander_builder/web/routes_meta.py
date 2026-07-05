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
import time
from pathlib import Path
from typing import Optional

from flask import Blueprint, Response, jsonify, render_template, request

from ._image_cache import ALLOWED_SIZES, serve_image


# Tunable for tests: how long to back off before retrying a 429.
_CARD_IMAGE_429_RETRY_DELAY_SEC = 1.5


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

    @bp.route("/api/card_image/<size>/<path:name>")
    def card_image(size: str, name: str):
        """Serve a cached Scryfall card image, fetching on cache miss.

        Before this route existed, every <img> in the audit panel hit
        Scryfall's ``cards/named?format=image`` redirect endpoint
        directly. A 40-card advisor output cascaded into 40 round-trips
        + 40 follow-redirects, stalling Chrome for 30-60s. Now the
        browser only ever talks to this route; we hit Scryfall once
        per ``(name, size)`` pair and serve every subsequent request
        from disk.

        ``size`` must be one of Scryfall's published version strings
        (small / normal / large / png / art_crop / border_crop);
        anything else returns 400. Name is URL-path-encoded so
        ``//`` separators in double-faced card names round-trip
        cleanly.

        ``Cache-Control: public, max-age=604800, immutable`` so the
        browser caches aggressively too — Scryfall image art for a
        given printing doesn't change after release.
        """
        if size not in ALLOWED_SIZES:
            return jsonify({
                "error": "unsupported size",
                "detail": f"size must be one of {sorted(ALLOWED_SIZES)}",
            }), 400
        # Scryfall 404 (unknown card) vs. transient failure (timeout, 5xx,
        # 429) — both surface as fetch errors. urllib's HTTPError exposes
        # .code; everything else is treated as a transient upstream issue.
        #
        # Retry ONCE on 429 (rate-limited) with a short backoff; if it
        # still fails, return 429 + Retry-After to the client rather than
        # an opaque 502 (audit-panel bursts used to surface as 9-up 502s
        # when Scryfall throttled them).
        last_exc: Optional[Exception] = None
        for attempt in (0, 1):
            try:
                data, content_type = serve_image(name, size)
                break
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None)
                if code == 404:
                    return jsonify({
                        "error": "card image not found",
                        "name": name,
                        "size": size,
                    }), 404
                if code == 429 and attempt == 0:
                    time.sleep(_CARD_IMAGE_429_RETRY_DELAY_SEC)
                    last_exc = exc
                    continue
                if code == 429:
                    resp = jsonify({
                        "error": "scryfall rate-limited",
                        "detail": f"{type(exc).__name__}: {exc}",
                    })
                    # 5s is a reasonable client-side backoff hint;
                    # Scryfall's docs ask for ~50-100ms per request.
                    resp.headers["Retry-After"] = "5"
                    return resp, 429
                return jsonify({
                    "error": "scryfall image fetch failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }), 502
        else:
            # Loop exhausted without break — only reachable if both
            # attempts hit 429 (other errors return early above). The
            # in-loop 429 branch returns; this defends if someone
            # changes the loop structure later.
            assert last_exc is not None
            return jsonify({
                "error": "scryfall rate-limited",
                "detail": f"{type(last_exc).__name__}: {last_exc}",
            }), 429
        resp = Response(data, mimetype=content_type)
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
        return resp

    @bp.route("/api/log_error", methods=["POST"])
    def log_error():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "expected JSON body"}), 400
        msg = (payload.get("message") or "").strip()
        if not msg:
            return jsonify({"error": "message is required"}), 400
        # Cap fields so a runaway browser doesn't bloat the log, and
        # strip newlines from the single-line fields so a crafted
        # payload can't forge fake log entries (log injection). The
        # multi-line stack keeps newlines but gets indented on write so
        # forged "---" separator lines can't masquerade as entries.
        def _one_line(value: str) -> str:
            return value.replace("\r", " ").replace("\n", " ")

        msg = _one_line(msg[:2000])
        url = _one_line((payload.get("url") or "")[:512])
        stack = (payload.get("stack") or "")[:4000]
        ua = _one_line((payload.get("user_agent") or "")[:256])
        kind = _one_line((payload.get("kind") or "error")[:40])
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
                    indented = "\n".join(
                        f"    {line}" for line in stack.splitlines()
                    )
                    f.write(f"STACK:\n{indented}\n")
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
