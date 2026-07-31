"""FP-016 replay-lite — persistence, retention, and flag-off tests.

Hard requirements pinned here:

- Flag OFF ⇒ byte-identical sim behavior: no replay dir is ever created,
  ``ForgeRunner.run``'s SimResult is unchanged.
- Bounded storage: total replays-dir size is capped with OLDEST-run
  eviction at write time; the in-flight run is never evicted but stops
  recording once it alone hits the cap (39GB-incident insurance).
- Thread safety: concurrent recorders (the threaded pod dispatcher)
  produce distinct files and an index that never tears or loses entries.
- CLI plumbing: ``--keep-logs`` on run_match / compare_versions sets the
  env flag before any sim launches.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from commander_builder import replay_store
from commander_builder.replay_store import (
    ENV_CAP_MB,
    ENV_KEEP_LOGS,
    ENV_REPLAY_DIR,
    INDEX_NAME,
    ReplayRun,
    enforce_retention,
    maybe_record_sim,
    replay_cap_bytes,
    replay_root,
    replays_enabled,
)

FIXTURES = Path(__file__).parent / "fixtures" / "replays"

DECKS = ["A.dck", "B.dck", "C.dck", "D.dck"]


def _one_game_stdout(winner: str = "Alpha", ms: int = 5000) -> str:
    return (
        f"Turn: Turn 1 (Ai(1)-{winner})\n"
        f"Life: Life: Ai(2)-Beta 40 > 0\n"
        f"Game Outcome: Turn 1\n"
        f"Game Outcome: Ai(2)-Beta has lost because life total reached 0\n"
        f"Game Result: Game 1 ended in {ms} ms. Ai(1)-{winner} has won!\n"
    )


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    """Isolated replay root + flag ON + fresh process-run."""
    root = tmp_path / "replays"
    monkeypatch.setenv(ENV_REPLAY_DIR, str(root))
    monkeypatch.setenv(ENV_KEEP_LOGS, "1")
    replay_store._reset_process_run_for_tests()
    yield root
    replay_store._reset_process_run_for_tests()


# ---------------------------------------------------------------------------
# Enable flag + defaults
# ---------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ENV_KEEP_LOGS, raising=False)
    assert replays_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("", False), ("no", False), ("off", False),
])
def test_enabled_values(monkeypatch, val, expected):
    monkeypatch.setenv(ENV_KEEP_LOGS, val)
    assert replays_enabled() is expected


def test_replay_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_REPLAY_DIR, str(tmp_path / "custom"))
    assert replay_root() == tmp_path / "custom"
    monkeypatch.delenv(ENV_REPLAY_DIR)
    assert replay_root() == Path.home() / ".commander-builder" / "replays"


def test_cap_default_and_override(monkeypatch):
    monkeypatch.delenv(ENV_CAP_MB, raising=False)
    assert replay_cap_bytes() == 500 * 1024 * 1024
    monkeypatch.setenv(ENV_CAP_MB, "10")
    assert replay_cap_bytes() == 10 * 1024 * 1024
    # Garbage / non-positive fall back to the default rather than 0
    # (a zero cap would silently disable persistence).
    monkeypatch.setenv(ENV_CAP_MB, "banana")
    assert replay_cap_bytes() == 500 * 1024 * 1024
    monkeypatch.setenv(ENV_CAP_MB, "-5")
    assert replay_cap_bytes() == 500 * 1024 * 1024


# ---------------------------------------------------------------------------
# Flag OFF ⇒ nothing happens
# ---------------------------------------------------------------------------

def test_flag_off_records_nothing(tmp_path, monkeypatch):
    root = tmp_path / "replays"
    monkeypatch.setenv(ENV_REPLAY_DIR, str(root))
    monkeypatch.delenv(ENV_KEEP_LOGS, raising=False)
    replay_store._reset_process_run_for_tests()
    written = maybe_record_sim(_one_game_stdout(), deck_filenames=DECKS)
    assert written == []
    assert not root.exists()


def test_flag_off_forge_runner_output_identical(tmp_path, monkeypatch):
    """Pin the flag-off contract at the ACTUAL seam: ForgeRunner.run with
    the flag unset must produce an identical SimResult and never create
    the replay dir."""
    from commander_builder import forge_runner as fr

    root = tmp_path / "replays"
    monkeypatch.setenv(ENV_REPLAY_DIR, str(root))
    monkeypatch.delenv(ENV_KEEP_LOGS, raising=False)
    replay_store._reset_process_run_for_tests()

    canned = _one_game_stdout()
    monkeypatch.setattr(
        fr, "_run_blocking",
        lambda cmd, timeout, cwd: (canned, "", 0, False, None),
    )
    runner = fr.ForgeRunner(
        java_path=Path("java"), forge_jar=Path("forge.jar"),
        forge_dir=tmp_path,
    )
    result = runner.run(DECKS, num_games=1)
    assert result.stdout == canned
    assert result.returncode == 0
    assert not root.exists()


def test_flag_on_forge_runner_records(tmp_path, monkeypatch):
    from commander_builder import forge_runner as fr

    root = tmp_path / "replays"
    monkeypatch.setenv(ENV_REPLAY_DIR, str(root))
    monkeypatch.setenv(ENV_KEEP_LOGS, "1")
    replay_store._reset_process_run_for_tests()

    canned = _one_game_stdout()
    monkeypatch.setattr(
        fr, "_run_blocking",
        lambda cmd, timeout, cwd: (canned, "", 0, False, None),
    )
    runner = fr.ForgeRunner(
        java_path=Path("java"), forge_jar=Path("forge.jar"),
        forge_dir=tmp_path,
    )
    result = runner.run(DECKS, num_games=1)
    assert result.stdout == canned  # recording never mutates the result
    logs = list(root.glob("*/game_*.log"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8") == canned.rstrip("\n")
    replay_store._reset_process_run_for_tests()


# ---------------------------------------------------------------------------
# Recording + index integrity
# ---------------------------------------------------------------------------

def test_record_multi_game_stdout_splits_files(replay_env):
    stdout = (FIXTURES / "multi_game.log").read_text(encoding="utf-8")
    written = maybe_record_sim(
        stdout, deck_filenames=DECKS, sim_duration_sec=200.0,
        source="unit_test",
    )
    assert len(written) == 3  # 2 complete + 1 trailing partial
    run_dir = written[0].parent
    assert sorted(p.name for p in run_dir.glob("game_*.log")) == [
        "game_1.log", "game_2.log", "game_3.log",
    ]
    index = json.loads((run_dir / INDEX_NAME).read_text(encoding="utf-8"))
    assert index["run"] == run_dir.name
    assert index["cap_reached"] is False
    games = index["games"]
    assert [g["game"] for g in games] == [1, 2, 3]
    assert all(g["decks"] == DECKS for g in games)
    assert all(g["source"] == "unit_test" for g in games)
    # Winner attribution comes from the existing parser vocabulary.
    assert games[0]["winner_name"] == "Alpha Ramp [B3]"
    assert games[0]["truncated"] is False
    assert games[1]["is_draw"] is True
    assert games[1]["winner_name"] is None
    assert games[2]["truncated"] is True
    # Eliminations recorded for post-game browsing without a re-parse.
    assert {e["seat"] for e in games[0]["eliminations"]} == {2, 3, 4}


def test_record_empty_or_noise_stdout_is_noop(replay_env):
    assert maybe_record_sim("", deck_filenames=DECKS) == []
    assert maybe_record_sim("   \n", deck_filenames=DECKS) == []
    assert maybe_record_sim(
        "boot noise only\nno games here\n", deck_filenames=DECKS,
    ) == []
    assert not replay_env.exists()


def test_successive_sims_share_one_process_run(replay_env):
    first = maybe_record_sim(_one_game_stdout("Alpha"), deck_filenames=DECKS)
    second = maybe_record_sim(_one_game_stdout("Beta"), deck_filenames=DECKS)
    assert first[0].parent == second[0].parent  # one run dir per process
    index = json.loads(
        (first[0].parent / INDEX_NAME).read_text(encoding="utf-8"))
    assert [g["game"] for g in index["games"]] == [1, 2]


def test_index_integrity_under_concurrent_writes(replay_env):
    """The threaded pod dispatcher records from many threads at once —
    every game must land as a distinct file and a distinct index entry."""
    run = ReplayRun(replay_env)
    n_threads = 12
    errors: list[BaseException] = []

    def _worker(i: int):
        try:
            run.record_stdout(
                _one_game_stdout(f"Deck{i}"),
                deck_filenames=DECKS,
                source=f"thread-{i}",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,))
               for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    files = sorted(run.dir.glob("game_*.log"))
    assert len(files) == n_threads
    index = json.loads((run.dir / INDEX_NAME).read_text(encoding="utf-8"))
    games = index["games"]
    assert len(games) == n_threads
    # Distinct, gapless game numbers; every file referenced exists.
    assert sorted(g["game"] for g in games) == list(range(1, n_threads + 1))
    assert {g["file"] for g in games} == {p.name for p in files}
    # No tmp file left behind by the atomic index replace.
    assert not (run.dir / (INDEX_NAME + ".tmp")).exists()


# ---------------------------------------------------------------------------
# Retention cap
# ---------------------------------------------------------------------------

def _fake_run(root: Path, name: str, size: int) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "game_1.log").write_bytes(b"x" * size)
    (d / INDEX_NAME).write_text('{"run": "%s", "games": []}' % name,
                                encoding="utf-8")
    return d


def test_enforce_retention_evicts_oldest_first(tmp_path):
    root = tmp_path / "replays"
    _fake_run(root, "20250101T000000Z_1_aaaaaa", 4000)
    _fake_run(root, "20250201T000000Z_1_bbbbbb", 4000)
    _fake_run(root, "20250301T000000Z_1_cccccc", 4000)
    evicted = enforce_retention(root, cap_bytes=9000)
    assert evicted == ["20250101T000000Z_1_aaaaaa"]
    assert not (root / "20250101T000000Z_1_aaaaaa").exists()
    assert (root / "20250301T000000Z_1_cccccc").exists()


def test_enforce_retention_never_evicts_kept_run(tmp_path):
    root = tmp_path / "replays"
    _fake_run(root, "20250101T000000Z_1_aaaaaa", 4000)
    _fake_run(root, "20250201T000000Z_1_bbbbbb", 4000)
    evicted = enforce_retention(
        root, cap_bytes=1000, keep_run_id="20250101T000000Z_1_aaaaaa")
    # The newer run goes; the protected (in-flight) one survives even
    # though the total still exceeds the cap.
    assert evicted == ["20250201T000000Z_1_bbbbbb"]
    assert (root / "20250101T000000Z_1_aaaaaa").exists()


def test_enforce_retention_under_cap_is_noop(tmp_path):
    root = tmp_path / "replays"
    _fake_run(root, "20250101T000000Z_1_aaaaaa", 100)
    assert enforce_retention(root, cap_bytes=10_000) == []
    assert (root / "20250101T000000Z_1_aaaaaa").exists()


def test_record_evicts_old_runs_at_write_time(replay_env, monkeypatch):
    # Two stale runs fill the (tiny) cap; a new recording must evict the
    # oldest to make room rather than grow past the cap.
    _fake_run(replay_env, "20200101T000000Z_1_aaaaaa", 3000)
    _fake_run(replay_env, "20200201T000000Z_1_bbbbbb", 3000)
    monkeypatch.setenv(ENV_CAP_MB, str(5000 / (1024 * 1024)))  # ~5KB cap
    written = maybe_record_sim(_one_game_stdout(), deck_filenames=DECKS)
    assert len(written) == 1
    assert not (replay_env / "20200101T000000Z_1_aaaaaa").exists()


def test_run_stops_recording_at_cap(replay_env, monkeypatch):
    run = ReplayRun(replay_env)
    first = run.record_stdout(_one_game_stdout(), deck_filenames=DECKS)
    assert len(first) == 1
    # Now shrink the cap below the run's current size: further games are
    # refused (the in-flight run is never evicted, so it must stop).
    monkeypatch.setenv(ENV_CAP_MB, str(10 / (1024 * 1024)))  # ~10 bytes
    second = run.record_stdout(_one_game_stdout(), deck_filenames=DECKS)
    assert second == []
    index = json.loads((run.dir / INDEX_NAME).read_text(encoding="utf-8"))
    assert index["cap_reached"] is True
    assert len(index["games"]) == 1  # earlier games preserved
    # ... and stays stopped even if the cap is raised mid-run (the flag
    # is sticky for the process; a fresh run records again).
    monkeypatch.setenv(ENV_CAP_MB, "500")
    assert run.record_stdout(_one_game_stdout(), deck_filenames=DECKS) == []


def test_maybe_record_never_raises(replay_env, monkeypatch):
    # Force an internal failure and confirm the sim path survives it.
    monkeypatch.setattr(
        replay_store.ReplayRun, "record_stdout",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert maybe_record_sim(_one_game_stdout(), deck_filenames=DECKS) == []


# ---------------------------------------------------------------------------
# CLI plumbing: --keep-logs sets the env flag
# ---------------------------------------------------------------------------

def test_run_match_keep_logs_flag_sets_env(monkeypatch):
    import os

    from commander_builder import run_match
    from commander_builder.run_match import MatchupReport

    monkeypatch.setenv(ENV_KEEP_LOGS, "0")  # registered for restore
    monkeypatch.setattr(
        run_match, "run_matchup",
        lambda **kw: MatchupReport(user_deck="x", bracket=3, timestamp="t"),
    )
    rc = run_match.main([
        "--user", "x.dck", "--bracket", "3", "--keep-logs",
    ])
    assert rc == 0
    assert os.environ[ENV_KEEP_LOGS] == "1"


def test_run_match_without_flag_leaves_env_alone(monkeypatch):
    import os

    from commander_builder import run_match
    from commander_builder.run_match import MatchupReport

    monkeypatch.delenv(ENV_KEEP_LOGS, raising=False)
    monkeypatch.setattr(
        run_match, "run_matchup",
        lambda **kw: MatchupReport(user_deck="x", bracket=3, timestamp="t"),
    )
    rc = run_match.main(["--user", "x.dck", "--bracket", "3"])
    assert rc == 0
    assert ENV_KEEP_LOGS not in os.environ


def test_compare_versions_keep_logs_flag_sets_env(monkeypatch):
    import os

    from commander_builder import compare_versions

    monkeypatch.setenv(ENV_KEEP_LOGS, "0")  # registered for restore
    monkeypatch.setattr(compare_versions, "compare", lambda **kw: object())
    monkeypatch.setattr(compare_versions, "_format_summary", lambda r: "ok")
    rc = compare_versions.main([
        "--old", "a.dck", "--new", "b.dck", "--bracket", "3", "--keep-logs",
    ])
    assert rc == 0
    assert os.environ[ENV_KEEP_LOGS] == "1"
