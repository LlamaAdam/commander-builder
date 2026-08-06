"""FP-016 replay-lite — opt-in persistence of raw Forge game logs.

DEFAULT OFF. When ``COMMANDER_BUILDER_KEEP_GAME_LOGS=1`` (or a CLI's
``--keep-logs`` flag, which just sets that env var), every Forge sim's
stdout is split into per-game chunks (``replay_timeline.split_games``) and
written under::

    ~/.commander-builder/replays/<run-id>/game_<n>.log
    ~/.commander-builder/replays/<run-id>/index.json

One ``<run-id>`` directory per PROCESS (created lazily on the first
recorded game): a soak / compare / run_match invocation is one run, and
the web viewer groups games by run. The id is timestamp-prefixed
(``YYYYMMDDTHHMMSSZ_<pid>_<hex>``) so lexicographic order == age order.

Hard requirements honored here:

- **Flag off ⇒ byte-identical sim behavior.** ``maybe_record_sim`` is the
  ONLY entry point the sim path calls; with the flag unset it returns
  after one env lookup and touches nothing on disk. It also never raises
  — a recording failure must never break a sim.
- **Bounded storage.** The repo once grew a 39GB log directory; unbounded
  growth is forbidden. Total replays-dir size is capped (default
  ~500MB, ``COMMANDER_BUILDER_REPLAY_CAP_MB`` overrides) with
  oldest-run eviction at write time. Eviction never touches the CALLER'S
  in-flight run, and skips any run with recent write activity (newest
  mtime within ``RECENT_RUN_GRACE_SEC``) — the caller's run id only
  protects against THIS process; the mtime guard is what keeps a
  concurrent process's in-flight run safe. If the caller's run alone
  reaches the cap, recording STOPS for the rest of the process
  (``cap_reached`` is flagged in its index) rather than growing without
  bound. A failed eviction (Windows file locks, permissions) is logged
  loudly and skipped, so the directory can temporarily exceed the cap
  until a later write retries.
- **Thread safety.** Parallel pods (compare_versions' threaded dispatch,
  run_ab_parallel's chunk threads) all funnel through one process-global
  run whose lock serializes file-number allocation and index writes —
  same idea as the sim-job sidecar discipline in ``web/routes_sim.py``
  (state on disk before it's observable). Index writes are
  atomic (tmp file + ``os.replace``) so a reader never sees a torn JSON.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENV_KEEP_LOGS = "COMMANDER_BUILDER_KEEP_GAME_LOGS"
ENV_REPLAY_DIR = "COMMANDER_BUILDER_REPLAY_DIR"
ENV_CAP_MB = "COMMANDER_BUILDER_REPLAY_CAP_MB"

DEFAULT_CAP_MB = 500
INDEX_NAME = "index.json"
_TRUTHY = ("1", "true", "yes", "on")

# A run whose newest file (or dir) mtime is this recent is presumed to be
# ANOTHER process's in-flight run and is never evicted: ``keep_run_id``
# only protects the calling process's own run, but parallel soaks /
# compares each hold their own process-global run in the same root.
# 30 min comfortably exceeds the gap between two writes of a live run
# (one sim's games land in a single ``record_stdout`` call).
RECENT_RUN_GRACE_SEC = 30 * 60


def replays_enabled() -> bool:
    """True when the operator opted into game-log persistence."""
    return os.environ.get(ENV_KEEP_LOGS, "").strip().lower() in _TRUTHY


def replay_root() -> Path:
    """Root directory for persisted replays (env-overridable for tests)."""
    env = os.environ.get(ENV_REPLAY_DIR)
    if env:
        return Path(env)
    return Path.home() / ".commander-builder" / "replays"


def replay_cap_bytes() -> int:
    """Total-size cap for the replays dir, in bytes (env-overridable)."""
    raw = os.environ.get(ENV_CAP_MB, "").strip()
    try:
        mb = float(raw) if raw else float(DEFAULT_CAP_MB)
    except ValueError:
        mb = float(DEFAULT_CAP_MB)
    if mb <= 0:
        mb = float(DEFAULT_CAP_MB)
    return int(mb * 1024 * 1024)


def _dir_size(path: Path) -> int:
    """Total bytes under ``path`` (best-effort; races with deletes)."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _newest_mtime(path: Path) -> float:
    """Newest mtime among ``path`` itself and its direct children.

    The activity signal for the eviction grace period: a live run's dir
    or its newest ``game_<n>.log`` / ``index.json`` was written moments
    ago. Direct children suffice — run dirs are flat. Best-effort: stat
    failures contribute nothing (0.0 == "ancient").
    """
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        pass
    try:
        for child in path.iterdir():
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return newest


def enforce_retention(
    root: Path,
    cap_bytes: int,
    keep_run_id: Optional[str] = None,
    now: Optional[float] = None,
) -> list[str]:
    """Evict oldest run dirs until the replays root fits under ``cap_bytes``.

    Run ids are timestamp-prefixed, so name order == age order; foreign
    directories an operator dropped in sort wherever their name lands and
    are treated the same (this dir belongs to the store).

    THE REAL GUARANTEE (not "the in-flight run is never evicted"):

    * The CALLER'S run (``keep_run_id``) is never evicted — but that id
      only names this process's run.
    * Any run with recent write activity (newest mtime within
      ``RECENT_RUN_GRACE_SEC`` of ``now``) is skipped too — that is what
      protects ANOTHER process's in-flight run sharing this root.
      ``now`` defaults to wall-clock time; tests inject a fake clock.
    * A failed eviction (Windows file locks, permissions) is logged
      loudly, skipped, and NOT counted as reclaimed — total size can
      therefore exceed the cap until a later write retries.

    Returns the evicted run ids. Never raises.
    """
    if now is None:
        now = time.time()
    if not root.is_dir():
        return []
    try:
        run_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return []
    sizes = {d: _dir_size(d) for d in run_dirs}
    total = sum(sizes.values())
    evicted: list[str] = []
    for d in run_dirs:  # oldest first
        if total <= cap_bytes:
            break
        if keep_run_id is not None and d.name == keep_run_id:
            continue
        if now - _newest_mtime(d) < RECENT_RUN_GRACE_SEC:
            continue  # recently active: presume a live run, never evict.
        try:
            shutil.rmtree(d)
            evicted.append(d.name)
            total -= sizes[d]
        except OSError as exc:
            logger.warning(
                "replay retention: failed to evict %s (%s) — replays dir "
                "may exceed its %d-byte cap until a later write retries",
                d, exc, cap_bytes,
            )
            continue
    return evicted


def _new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{os.getpid()}_{secrets.token_hex(3)}"


class ReplayRun:
    """One run directory: allocates game numbers, writes logs + index.

    All mutation happens under ``self._lock`` so concurrent pod threads
    write distinct ``game_<n>.log`` files and the index never tears.
    """

    def __init__(self, root: Path, run_id: Optional[str] = None) -> None:
        self.run_id = run_id or _new_run_id()
        self.root = root
        self.dir = root / self.run_id
        self._lock = threading.Lock()
        self._next_game = 1
        self._cap_reached = False

    # -- internals (call with the lock held) --------------------------------

    def _load_index(self) -> dict:
        idx_path = self.dir / INDEX_NAME
        if idx_path.is_file():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("games"), list):
                    return data
            except (OSError, ValueError):
                pass
        return {
            "run": self.run_id,
            "created": datetime.now(timezone.utc).isoformat(),
            "format_version": 1,
            "cap_reached": False,
            "games": [],
        }

    def _write_index(self, index: dict) -> None:
        """Atomic replace so a concurrent reader never sees a torn file."""
        idx_path = self.dir / INDEX_NAME
        tmp_path = self.dir / (INDEX_NAME + ".tmp")
        tmp_path.write_text(
            json.dumps(index, indent=2, default=str), encoding="utf-8",
        )
        os.replace(tmp_path, idx_path)

    # -- public -------------------------------------------------------------

    def record_stdout(
        self,
        stdout: str,
        *,
        deck_filenames: list[str],
        game_format: str = "commander",
        sim_duration_sec: float = 0.0,
        source: str = "sim",
    ) -> list[Path]:
        """Split ``stdout`` into games and persist each. Returns paths written.

        A trailing partial game (timeout/abort kill) is persisted too,
        indexed with ``truncated: true``. Winner/elimination metadata in
        the index comes from ``replay_timeline.parse_timeline`` — which
        reuses the EXISTING log_parser/game_analyzer attribution
        vocabulary, so the index can't disagree with match scoring.
        """
        from .replay_timeline import parse_timeline, split_games

        chunks = split_games(stdout)
        if not chunks:
            return []
        written: list[Path] = []
        with self._lock:
            if self._cap_reached:
                return []
            cap = replay_cap_bytes()
            self.root.mkdir(parents=True, exist_ok=True)
            # Evict oldest OTHER runs first so new data always has room;
            # then check whether this run alone has consumed the budget.
            enforce_retention(self.root, cap, keep_run_id=self.run_id)
            if self.dir.exists() and _dir_size(self.dir) >= cap:
                self._cap_reached = True
                index = self._load_index()
                index["cap_reached"] = True
                self._write_index(index)
                return []
            self.dir.mkdir(parents=True, exist_ok=True)
            index = self._load_index()
            recorded_at = datetime.now(timezone.utc).isoformat()
            for chunk in chunks:
                game_n = self._next_game
                self._next_game += 1
                filename = f"game_{game_n}.log"
                path = self.dir / filename
                path.write_text(chunk["text"], encoding="utf-8")
                timeline = parse_timeline(chunk["text"])
                result = timeline["result"]
                index["games"].append({
                    "game": game_n,
                    "file": filename,
                    "decks": list(deck_filenames),
                    "game_format": game_format,
                    "source": source,
                    "recorded_at": recorded_at,
                    "sim_duration_sec": round(float(sim_duration_sec), 1),
                    "winner_seat": result["winner_seat"],
                    "winner_name": result["winner_name"],
                    "end_turn": result["end_turn"],
                    # Two DIFFERENT counters — see the turn-count
                    # convention in replay_timeline.parse_timeline.
                    # Indexes written before 2026-08 lack both keys;
                    # readers must treat a missing value as None and
                    # fall back to end_turn.
                    "end_round": result["end_round"],
                    "player_turns": result["player_turns"],
                    "duration_ms": result["duration_ms"],
                    "is_draw": result["is_draw"],
                    "eliminations": result["eliminations"],
                    "truncated": timeline["truncated"],
                })
                written.append(path)
            self._write_index(index)
        return written


# ---------------------------------------------------------------------------
# Process-global run — the seam forge_runner.run() records through.
# ---------------------------------------------------------------------------

_process_run: Optional[ReplayRun] = None
_process_run_lock = threading.Lock()


def _get_process_run() -> ReplayRun:
    """The one ReplayRun for this process (created lazily, thread-safe)."""
    global _process_run
    if _process_run is None or _process_run.root != replay_root():
        with _process_run_lock:
            if _process_run is None or _process_run.root != replay_root():
                _process_run = ReplayRun(replay_root())
    return _process_run


def _reset_process_run_for_tests() -> None:
    global _process_run
    with _process_run_lock:
        _process_run = None


def maybe_record_sim(
    stdout: str,
    *,
    deck_filenames: list[str],
    game_format: str = "commander",
    sim_duration_sec: float = 0.0,
    source: str = "forge_runner",
) -> list[Path]:
    """Record one sim's stdout into the process run — IF the flag is on.

    The single call site is the tail of ``ForgeRunner.run``. Flag off ⇒
    one env read, zero filesystem access, empty return. Never raises:
    replay capture is strictly best-effort and must never break a sim.
    """
    if not replays_enabled():
        return []
    if not stdout or not stdout.strip():
        return []
    try:
        run = _get_process_run()
        return run.record_stdout(
            stdout,
            deck_filenames=deck_filenames,
            game_format=game_format,
            sim_duration_sec=sim_duration_sec,
            source=source,
        )
    except Exception:  # noqa: BLE001 — never let recording break a sim
        return []
