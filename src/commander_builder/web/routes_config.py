"""Per-user config endpoint for the web layer (FP-011).

Two routes backing the Settings panel:

    GET  /api/config   -> redacted config (token never echoed; a
                          ``<key>_set`` flag + last-4 ``<key>_hint``
                          stand in for each secret).
    PUT  /api/config   -> validate a sparse update, merge into the
                          stored config, persist owner-only, return the
                          redacted result. 400 on any validation error
                          (nothing is persisted on a bad request).

Permissions posture: the web app binds to 127.0.0.1 (see
``web/app.main``), so the PUT surface is reachable only from the local
machine; the on-disk file is written 0o600. The raw Anthropic token is
never returned by GET, so it can't leak through the browser.

Stateless factory — config location resolves via
``config_store.config_path()`` (honoring ``COMMANDER_BUILDER_CONFIG``),
so the blueprint needs no constructor args.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import config_store


def make_config_blueprint() -> Blueprint:
    """Build the Flask Blueprint exposing GET/PUT ``/api/config``."""
    bp = Blueprint("config", __name__)

    @bp.route("/api/config", methods=["GET"])
    def get_config():
        cfg = config_store.load_config()
        return jsonify(config_store.redact_config(cfg))

    @bp.route("/api/config", methods=["PUT"])
    def put_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({
                "error": "request body must be a JSON object",
            }), 400

        normalized, errors = config_store.validate_update(body)
        if errors:
            # Reject the whole request — partial writes would leave the
            # config in a state the client didn't ask for.
            return jsonify({"error": "validation failed", "details": errors}), 400

        cfg = config_store.apply_update(normalized)
        return jsonify({
            "status": "ok",
            "updated": sorted(normalized.keys()),
            "config": config_store.redact_config(cfg),
        })

    return bp
