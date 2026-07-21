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

from pathlib import Path

from flask import Blueprint, jsonify, request

from ._helpers import (
    _bracket_from_filename,
    _normalize_pasted_deck,
    _resolve_deck_path,
)


_BRACKET_NAMES = {
    1: "Exhibition", 2: "Core", 3: "Upgraded", 4: "Optimized", 5: "cEDH",
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
              ``{"name": "<display>", "paste_text": "<.dck or moxfield-format>"}``

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
