"""FP-006 Flask scaffold — serves the deck-dashboard JSON feed and a
placeholder root page.

Routes:
    GET  /                          -> placeholder HTML (no design yet)
    GET  /api/health                -> {"status": "ok"}
    GET  /api/decks                 -> {"decks": [{"id", "name", "path"}, ...]}
    GET  /api/dashboard?deck=<id>   -> DashboardData JSON for that deck
    GET  /api/dashboard?path=<p>    -> DashboardData JSON for an explicit path

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


def _list_decks(deck_dir: Path) -> list[dict]:
    """Enumerate ``.dck`` files under ``deck_dir`` (non-recursive)."""
    if not deck_dir.exists() or not deck_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(deck_dir.glob("*.dck")):
        out.append({
            "id": p.stem,
            "name": p.stem,
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


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Commander Builder</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 4rem auto; padding: 0 1rem; color: #222; }
    code { background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; }
    .endpoints li { margin-bottom: 0.4rem; }
  </style>
</head>
<body>
  <h1>Commander Builder</h1>
  <p>FP-006 backend scaffold is live. UI rendering not implemented yet.</p>
  <p>Try the JSON feed:</p>
  <ul class="endpoints">
    <li><code>GET /api/health</code></li>
    <li><code>GET /api/decks</code></li>
    <li><code>GET /api/dashboard?deck=&lt;id&gt;</code></li>
  </ul>
</body>
</html>
"""


def create_app(deck_dir: Optional[Path] = None):
    """Build the Flask app. Imports flask lazily so the rest of
    commander_builder works without the ``[web]`` extra installed."""
    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:
        raise RuntimeError(
            "flask is required for the web scaffold. "
            "Install with: pip install commander-builder[web]"
        ) from exc

    if deck_dir is None:
        env_dir = os.environ.get("COMMANDER_BUILDER_DECK_DIR")
        deck_dir = Path(env_dir) if env_dir else Path.cwd() / "decks"
    deck_dir = deck_dir.resolve()

    app = Flask(__name__)
    app.config["DECK_DIR"] = deck_dir

    @app.route("/")
    def root():
        return _PLACEHOLDER_HTML

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "deck_dir": str(deck_dir),
            "deck_count": len(_list_decks(deck_dir)),
        })

    @app.route("/api/decks")
    def decks():
        return jsonify({"decks": _list_decks(deck_dir)})

    @app.route("/api/dashboard")
    def dashboard():
        deck_id = request.args.get("deck")
        explicit = request.args.get("path")
        try:
            bracket_raw = request.args.get("bracket")
            bracket = int(bracket_raw) if bracket_raw else None
        except ValueError:
            return jsonify({"error": "bracket must be an integer 1..5"}), 400

        path = _resolve_deck_path(deck_dir, deck_id, explicit)
        if path is None:
            return jsonify({
                "error": "deck not found",
                "deck": deck_id,
                "path": explicit,
            }), 404

        data = build_dashboard(path, bracket=bracket)
        return jsonify(data.to_dict())

    return app


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
