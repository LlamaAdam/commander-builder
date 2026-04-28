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
from ..knowledge_log import (
    DEFAULT_DB_PATH as _DEFAULT_KLOG_DB,
    get_iteration,
    iterations_for_deck,
    recent_iterations,
)


def _list_decks(deck_dir: Path, user_only: bool = True) -> list[dict]:
    """Enumerate ``.dck`` files under ``deck_dir`` (non-recursive).

    By default returns only ``[USER] *.dck`` files — those are the
    decks under active iteration. Set ``user_only=False`` to also
    list filler / pool decks (used by curation commands)."""
    if not deck_dir.exists() or not deck_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(deck_dir.glob("*.dck")):
        if user_only and not p.stem.startswith("[USER]"):
            continue
        # Display name: strip the [USER] prefix and any trailing
        # bracket version like " [B3]" so the sidebar reads cleanly.
        import re as _re
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
        deck_dir = Path(env_dir) if env_dir else Path.cwd() / "decks"
    deck_dir = deck_dir.resolve()

    if knowledge_db is None:
        env_db = os.environ.get("COMMANDER_BUILDER_KNOWLEDGE_DB")
        knowledge_db = Path(env_db) if env_db else _DEFAULT_KLOG_DB
    knowledge_db = Path(knowledge_db)

    app = Flask(__name__)
    app.config["DECK_DIR"] = deck_dir
    app.config["KNOWLEDGE_DB"] = knowledge_db

    @app.route("/")
    def root():
        return render_template("index.html")

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "deck_dir": str(deck_dir),
            "deck_count": len(_list_decks(deck_dir)),
        })

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

        try:
            from ..improvement_advisor import advise as _advise
            report = _advise(path, bracket=bracket)
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
            }
            for rec in report.recommendations
            if rec.action == "add" and rec.card.lower() in applied_add_set
        ]
        removed_payload = [
            {"card": rec.card, "rationale": rec.reason or ""}
            for rec in report.recommendations
            if rec.action == "cut" and rec.card.lower() in applied_cut_set
        ]
        return jsonify({
            "deck": deck_id,
            "bracket": bracket,
            "proposed_text": proposed_text,
            "added": added_payload,
            "removed": removed_payload,
            "kept_count": kept,
            "main_count": kept + len(added_payload),
            "diagnosis": getattr(report.diagnosis, "pattern_summary", ""),
            "weakness_signals": list(getattr(
                report.diagnosis, "weakness_signals", [],
            ) or []),
            "source": getattr(report, "source", "heuristic"),
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
        mode = payload.get("mode", "1v1")
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

        # Stage the proposed deck as a sibling of the old one so Forge
        # can find it via DECK_DIR. Suffix it _proposed_<timestamp> to
        # avoid collisions.
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        new_path = old_path.parent / f"{old_path.stem}_proposed_{ts}.dck"
        try:
            new_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": f"could not stage new deck: {exc}"}), 500

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
            report = compare(
                old_deck=old_path.name,
                new_deck=new_path.name,
                bracket=bracket,
                games_per_pod=games,
                # In pod mode, default 2 filler pairs is fine; 1v1 ignores it.
                filler_pairs=2,
                mode=mode,
                runner=runner,
            )
        except Exception as exc:  # pragma: no cover - Forge runtime errors
            try:
                new_path.unlink()
            except OSError:
                pass
            return jsonify({
                "error": "compare failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 500

        # Don't leak the temp staged file — but DO keep it if the user
        # might want to commit-the-swap later. Compromise: keep the file
        # under a `_proposed/` subdir but return a "cleanup hint" the
        # UI can act on.
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
    uses the same 1..100 scale the suggestion panel renders."""
    if not evidence:
        return 0
    inclusion = float(evidence.get("inclusion_pct") or 0)
    synergy = min(float(evidence.get("synergy_pct") or 0), 20.0)
    raw = inclusion + synergy
    return max(1, min(100, round(raw)))


def _format_added_line(name: str) -> str:
    """Render a `1 <Name>|<SET>|<CN>` line for an added card.

    Forge's deck loader can be strict about ambiguous name-only
    lookups (alternate art, reprints across many sets, special
    characters like //). We resolve each appended card to its
    current Scryfall printing so the proposed deck loads cleanly.
    Falls back to plain `1 <name>` when Scryfall doesn't return
    usable set/cn info — better to ship a slightly-ambiguous line
    than crash the whole audit on a network blip.
    """
    try:
        from ..scryfall_client import lookup_card
        data = lookup_card(name) or {}
    except Exception:
        return f"1 {name}"
    set_code = (data.get("set") or "").upper()
    cn = data.get("collector_number") or ""
    if set_code and cn:
        return f"1 {name}|{set_code}|{cn}"
    return f"1 {name}"


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
