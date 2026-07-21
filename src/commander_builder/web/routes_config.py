"""Per-user config endpoints for the web layer (FP-011 + collection).

Routes backing the Settings panel:

    GET  /api/config     -> redacted config (token never echoed; a
                            ``<key>_set`` flag + last-4 ``<key>_hint``
                            stand in for each secret).
    PUT  /api/config     -> validate a sparse update, merge into the
                            stored config, persist owner-only, return the
                            redacted result. 400 on any validation error
                            (nothing is persisted on a bad request).
    GET  /api/collection -> registered card-collection status
                            ``{configured, count, path}`` — never the
                            full list (it can be tens of thousands of
                            lines; the Settings panel only needs the
                            headline, and users edit by re-pasting).
    PUT  /api/collection -> body ``{"text": "..."}`` — import a pasted
                            collection (CSV with a Name column, or
                            plain lines / '<qty> Name'; parsing reuses
                            ``collection.parse_collection_text`` which
                            reuses ``import_formats``' CSV machinery).
                            Empty text CLEARS the registration (deletes
                            the file — restores the filters-inert
                            state). 400 with the offending line on a
                            malformed CSV row; nothing is persisted.

The collection lives at ``~/.commander-builder/collection.txt`` (env-
overridable), NEVER in the repo — see ``collection.py``'s module
docstring for the path contract.

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

from .. import collection as collection_store
from .. import config_store
from ..import_formats import ImportFormatError


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

    @bp.route("/api/collection", methods=["GET"])
    def get_collection():
        """Collection status headline for the Settings panel.

        ``configured`` distinguishes "no file" (filters inert) from a
        registered collection; ``count`` is the number of distinct
        name-keys. The path is included so the panel can tell the user
        where the file lives for manual edits.
        """
        keys = collection_store.load_collection()
        return jsonify({
            "configured": keys is not None,
            "count": len(keys) if keys is not None else 0,
            "path": str(collection_store.collection_path()),
        })

    @bp.route("/api/collection", methods=["PUT"])
    def put_collection():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return jsonify({
                "error": "request body must be a JSON object with a "
                         "string 'text' field",
            }), 400
        text = body["text"]
        if not text.strip():
            # Empty paste = unregister. Deleting (not truncating) the
            # file restores load_collection()'s None sentinel so every
            # ownership filter goes back to inert — a truncated file
            # would instead mean "I own nothing" and flag everything.
            collection_store.clear_collection()
            return jsonify({
                "status": "ok", "configured": False, "count": 0,
            })
        try:
            names = collection_store.parse_collection_text(text)
        except ImportFormatError as exc:
            # Malformed row in a positively-detected CSV — surface the
            # line verbatim (the error message embeds it), persist
            # nothing. Same contract as the deck-paste import route.
            return jsonify({"error": str(exc)}), 400
        if not names:
            # Non-empty text that parsed to zero names (all comments /
            # header-only CSV) is almost certainly a user mistake —
            # reject rather than silently registering an empty
            # collection that would flag every suggestion as unowned.
            return jsonify({
                "error": "no card names found in the pasted text",
            }), 400
        collection_store.save_collection(names)
        return jsonify({
            "status": "ok", "configured": True, "count": len(names),
        })

    return bp
