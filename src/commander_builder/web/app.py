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

# ``detect_forge_version`` is still needed at app boot for the
# Forge-jar staleness check that prints to stdout. The per-blueprint
# route modules import the rest of the heavyweight deps themselves
# (build_dashboard, knowledge_log functions, etc.) so they don't
# need re-importing here.
from ..forge_runner import detect_forge_version
# Module import (not ``from ..knowledge_log import DEFAULT_DB_PATH``) so the
# default DB path is read at app-creation time — a from-import would freeze
# the value at import time and bypass the test suite's isolation patch.
from .. import knowledge_log as _knowledge_log

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
    _resolve_deck_path,
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


def create_app(
    deck_dir: Optional[Path] = None,
    knowledge_db: Optional[Path] = None,
):
    """Build the Flask app. Imports flask lazily so the rest of
    commander_builder works without the ``[web]`` extra installed."""
    try:
        from flask import Flask
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
        knowledge_db = Path(env_db) if env_db else _knowledge_log.DEFAULT_DB_PATH
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
    from .routes_cards import make_cards_blueprint
    from .routes_config import make_config_blueprint
    from .routes_dashboard import make_dashboard_blueprint
    from .routes_decks import make_decks_blueprint
    from .routes_library import make_library_blueprint
    from .routes_meta import make_meta_blueprint
    from .routes_oracle import make_oracle_blueprint
    from .routes_rules import make_rules_blueprint
    from .routes_sim import make_sim_blueprint
    app.register_blueprint(make_audit_blueprint(deck_dir))
    app.register_blueprint(make_dashboard_blueprint(
        deck_dir, knowledge_db, _list_decks,
    ))
    app.register_blueprint(make_decks_blueprint(deck_dir))
    # FP-007 slice 2: cross-deck library search (which decks run a card?).
    app.register_blueprint(make_library_blueprint(deck_dir))
    app.register_blueprint(
        make_meta_blueprint(deck_dir, _list_decks, _ASSET_VERSION),
    )
    # FP-009: oracle-text presentation endpoint backing the audit
    # panel's hover tooltip + click-to-expand side panel.
    app.register_blueprint(make_oracle_blueprint())
    # FP-007 (unified app, slice 1): card-reference panel — richer
    # projection (identity / legality / price / printing) behind the
    # topbar "Cards" search box.
    app.register_blueprint(make_cards_blueprint())
    # FP-007 slice 3: combo + bracket rules lookup.
    app.register_blueprint(make_rules_blueprint())
    # FP-011: per-user config (redacted GET / restricted PUT) backing
    # the Settings panel — BYO Anthropic token + app preferences.
    app.register_blueprint(make_config_blueprint())
    app.register_blueprint(make_sim_blueprint(deck_dir, knowledge_db))


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
