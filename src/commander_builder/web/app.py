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

    @app.route("/api/advise")
    def advise_route():
        """Standalone advise endpoint — same shape as /api/dashboard's
        suggested_adds but doesn't require a full dashboard rebuild."""
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else 3
        except ValueError:
            return jsonify({"error": "bracket must be an integer 1..5"}), 400

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

    @app.route("/api/deck_text")
    def deck_text():
        """Return the raw .dck text for a deck. Used by the
        'Propose changes' UI to pre-populate the modify-deck editor."""
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({
                "error": "deck not found",
                "deck": deck_id, "path": explicit,
            }), 404
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "deck": deck_id,
            "path": str(path),
            "text": text,
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
