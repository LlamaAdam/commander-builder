"""FP-016 replay-lite — web endpoints for browsing persisted game replays.

Two read-only routes over the ``~/.commander-builder/replays/`` store that
``replay_store`` writes when ``COMMANDER_BUILDER_KEEP_GAME_LOGS=1``:

``GET /api/replays``

    Enumerate runs (newest first) from each run dir's ``index.json``.
    Response::

        {
          "runs": [
            {
              "run": str,               # run dir name (validated id)
              "created": str | null,    # ISO timestamp
              "cap_reached": bool,      # run stopped recording at the cap
              "count": int,
              "games": [
                {"game", "decks", "game_format", "source", "winner_seat",
                 "winner_name", "end_turn", "end_round", "player_turns",
                 "duration_ms", "is_draw", "truncated"},
                ...
              ]
            }, ...
          ],
          "count": int,
          "enabled": bool               # is capture currently switched on
        }

``GET /api/replay/<run_id>/<int:game>``

    Parse ``game_<n>.log`` through ``replay_timeline.parse_timeline`` and
    return ``{"run", "game", "meta": <index entry>, "timeline": {...}}``.
    Clean JSON 404 on unknown run/game.

Path safety: ``run_id`` must match a strict allowlist pattern (no
separators, no dots-only components) AND resolve to a direct child of the
replays root; the game log filename is re-derived from the validated game
NUMBER (never taken from the request or the index), so neither route can
read outside the store. Responses are always ``application/json``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify

from ..replay_store import INDEX_NAME, replay_root, replays_enabled

# Run ids are `<ts>_<pid>_<hex>` (see replay_store._new_run_id) but the
# validator accepts any single flat path component of safe characters so
# hand-named dirs still browse. Explicitly excludes path separators; the
# dots-only forms (".", "..") are rejected by requiring a word character.
_RUN_ID_RE = re.compile(r"^(?=.{1,120}$)[A-Za-z0-9._-]*\w[A-Za-z0-9._-]*$")

# Fields from an index game entry that the list endpoint exposes.
# ``end_round`` / ``player_turns`` are the two disambiguated turn
# counters (see replay_timeline's turn-count convention). They are None
# for runs indexed before 2026-08 — ``.get`` below yields None and the
# UI falls back to the legacy ``end_turn``.
_GAME_SUMMARY_FIELDS = (
    "game", "decks", "game_format", "source", "winner_seat", "winner_name",
    "end_turn", "end_round", "player_turns", "duration_ms", "is_draw",
    "truncated",
)

_MAX_GAME_NUMBER = 100_000


def _safe_run_dir(run_id: str) -> Optional[Path]:
    """Resolve ``run_id`` to a run dir inside the replays root, or None.

    Belt (allowlist regex — no separators or traversal tokens) and
    suspenders (resolved path must be a DIRECT child of the resolved
    root) — mirrors the `_resolve_deck_path` discipline in `_helpers`.
    """
    if not _RUN_ID_RE.match(run_id) or ".." in run_id:
        return None
    try:
        root = replay_root().resolve()
        candidate = (root / run_id).resolve()
    except OSError:
        return None
    if candidate.parent != root:
        return None
    return candidate if candidate.is_dir() else None


def _load_index(run_dir: Path) -> Optional[dict]:
    idx = run_dir / INDEX_NAME
    if not idx.is_file():
        return None
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        return None
    return data


def make_replays_blueprint() -> Blueprint:
    """Read-only Blueprint for ``/api/replays`` + ``/api/replay/...``.

    Stateless — the replays root is re-read per request (env-aware), so
    tests and long-lived servers both see the live store.
    """
    bp = Blueprint("replays", __name__)

    @bp.route("/api/replays")
    def list_replays():
        root = replay_root()
        runs: list[dict] = []
        if root.is_dir():
            try:
                run_dirs = sorted(
                    (d for d in root.iterdir() if d.is_dir()),
                    reverse=True,  # ids are timestamp-prefixed: newest first
                )
            except OSError:
                run_dirs = []
            for d in run_dirs:
                if not _RUN_ID_RE.match(d.name):
                    continue  # foreign dir the API can't safely address
                index = _load_index(d)
                if index is None:
                    continue
                games = [
                    {k: g.get(k) for k in _GAME_SUMMARY_FIELDS}
                    for g in index["games"]
                    if isinstance(g, dict)
                ]
                runs.append({
                    "run": d.name,
                    "created": index.get("created"),
                    "cap_reached": bool(index.get("cap_reached")),
                    "count": len(games),
                    "games": games,
                })
        return jsonify({
            "runs": runs,
            "count": len(runs),
            "enabled": replays_enabled(),
        })

    @bp.route("/api/replay/<run_id>/<int:game>")
    def get_replay(run_id: str, game: int):
        run_dir = _safe_run_dir(run_id)
        if run_dir is None:
            return jsonify({"error": f"unknown replay run: {run_id!r}"}), 404
        if game < 1 or game > _MAX_GAME_NUMBER:
            return jsonify({"error": f"invalid game number: {game}"}), 404
        index = _load_index(run_dir)
        meta = None
        if index is not None:
            meta = next(
                (g for g in index["games"]
                 if isinstance(g, dict) and g.get("game") == game),
                None,
            )
        # Filename derived from the VALIDATED integer only — never from
        # the request path or the (on-disk, editable) index contents.
        log_path = run_dir / f"game_{game}.log"
        if meta is None or not log_path.is_file():
            return jsonify({
                "error": f"no game {game} in replay run {run_id!r}",
            }), 404
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return jsonify({
                "error": f"replay log unreadable for game {game}",
            }), 404
        from ..replay_timeline import parse_timeline
        return jsonify({
            "run": run_id,
            "game": game,
            "meta": meta,
            "timeline": parse_timeline(text),
        })

    return bp
