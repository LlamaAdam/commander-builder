"""Tests for scripts/soak_pool.py's per-sim row recording and its
failure-storm protection (StormBreaker: per-runner backoff, pool-wide
circuit breaker with canary probes, row suppression at source).

The StormBreaker tests run on a fake clock injected through the
``now``/``sleep`` seams — no Forge, no threads, no real sleeps.

The recorder tests focus on the gauntlet recorder's status semantics: a
'loop_unattributed' GauntletResult (batch cut short by a looping game
that no seat could be credited for — Forge prints the game log only
after a game completes, so a hung game leaves no Turn line to
attribute) is an honest SHORT row whose completed games are real data.
It must count toward sims_done/games_done like a 'done' row, not be
booked as a failure, and the JSONL row must carry the distinct status
verbatim so downstream consumers (margin_analysis, merge_soak) can tell
a legitimately short row from a genuine error.

No Forge, no threads: `_record_gauntlet` is exercised on a minimal
stand-in `self` (it only touches the lock, the counters, and args).
"""
from __future__ import annotations

import json
import threading
import types
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import soak_pool  # noqa: E402

from commander_builder.forge_batch import GauntletResult  # noqa: E402


def _soak_stub(tmp_path: Path):
    """Minimal object satisfying everything _record_gauntlet reads."""
    s = types.SimpleNamespace()
    s.lock = threading.Lock()
    s.sims_done = 0
    s.sims_failed = 0
    s.games_done = 0
    s.wins_a = 0
    s.wins_b = 0
    s.args = types.SimpleNamespace(label="testhost",
                                   out=tmp_path / "rows.jsonl")
    s.args.out.write_text("", encoding="utf-8")
    return s


def _read_rows(out: Path) -> list[dict]:
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_record_gauntlet_counts_loop_unattributed_as_completed(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D v2 [B3].dck",
        gauntlet=["G1.dck", "G2.dck", "G3.dck"],
        games=17, wins=5, losses=11, draws=1,
        status="loop_unattributed",
        error=("loop at game 18: no seat attributable from partial stdout "
               "(Forge prints the game log only after a game completes); "
               "kept 17 completed games"),
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D v2 [B3].dck")

    assert s.sims_done == 1          # counted as data, NOT a failure
    assert s.sims_failed == 0
    assert s.games_done == 17        # only the completed games
    assert s.wins_a == 5 and s.wins_b == 11

    (row,) = _read_rows(s.args.out)
    assert row["status"] == "loop_unattributed"   # distinct status, verbatim
    assert row["games"] == 17
    assert row["role"] == "v2"
    assert "kept 17 completed games" in row["error"]


def test_record_gauntlet_still_books_real_failures_as_failed(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D [B3].dck",
        games=2, wins=1, losses=1,
        status="failed", error="Forge exited with code 1",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck")

    assert s.sims_done == 0
    assert s.sims_failed == 1
    assert s.games_done == 0

    (row,) = _read_rows(s.args.out)
    assert row["status"] == "failed"


def test_record_gauntlet_done_row_unchanged(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D [B3].dck",
        games=40, wins=12, losses=26, draws=2, status="done",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck")

    assert s.sims_done == 1 and s.games_done == 40
    (row,) = _read_rows(s.args.out)
    assert row["status"] == "done" and row["role"] == "base"


# --- StormBreaker: backoff / circuit breaker / row suppression ----------
#
# Guards against the 2026-07-24 incident: a logged-off session made every
# Forge JVM launch fail instantly (exit 3221225794) and the un-throttled
# pool wrote ~85M sub-second failure rows (39 GB) in ~2 days.


class _Clock:
    """Fake monotonic clock: sleeping advances time instantly."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.t += sec


def _breaker(clock: _Clock, **kw):
    logs: list[str] = []
    b = soak_pool.StormBreaker(now=clock.now, sleep=clock.sleep,
                               log=logs.append, **kw)
    return b, logs


def _fail_instant(b, profile="r1", n=1):
    for _ in range(n):
        b.record(profile, ok=False, duration_sec=0.02)


def test_backoff_schedule_doubles_and_caps():
    clock = _Clock()
    b, _ = _breaker(clock)
    # 5 -> 10 -> 20 -> 40 -> 80 -> 160 -> capped at 300 (8 failures stays
    # below the 10-consecutive breaker threshold, so this is pure backoff).
    for expected in (5, 10, 20, 40, 80, 160, 300, 300):
        _fail_instant(b, "r1")
        before = sum(clock.sleeps)
        assert b.pre_run_wait("r1") is False
        assert sum(clock.sleeps) - before == expected
    assert not b.is_open


def test_backoff_resets_on_success():
    clock = _Clock()
    b, _ = _breaker(clock)
    _fail_instant(b, "r1", n=3)
    b.record("r1", ok=True, duration_sec=120.0)
    before = len(clock.sleeps)
    assert b.pre_run_wait("r1") is False
    assert clock.sleeps[before:] == []  # no backoff after a success


def test_breaker_opens_after_consecutive_instant_failures_across_pool():
    clock = _Clock()
    b, logs = _breaker(clock)
    # Alternate two runners: the threshold is POOL-wide, not per-runner.
    for i in range(9):
        _fail_instant(b, f"r{i % 2 + 1}")
    assert not b.is_open
    assert b.record("r1", ok=False, duration_sec=0.02) == "write"  # 10th
    assert b.is_open
    assert b.open_count == 1
    assert any("BREAKER OPEN" in m for m in logs)


def test_slow_failure_breaks_the_consecutive_chain():
    clock = _Clock()
    b, _ = _breaker(clock)
    _fail_instant(b, "r1", n=9)
    # A slow failure means the JVM genuinely ran: not a launch storm.
    b.record("r1", ok=False, duration_sec=120.0)
    _fail_instant(b, "r1", n=9)
    assert not b.is_open
    _fail_instant(b, "r1")  # 10th consecutive instant one
    assert b.is_open


def test_canary_probe_after_interval_and_success_closes():
    clock = _Clock()
    b, logs = _breaker(clock)
    _fail_instant(b, "r1", n=10)
    assert b.is_open
    opened_at = clock.t
    # A runner asking to work while open waits out the canary interval,
    # then gets the (single) canary probe.
    assert b.pre_run_wait("r2") is True
    assert clock.t - opened_at >= 900.0
    # A second runner never gets a probe while one is in flight; it just
    # keeps waiting until told to stop.
    assert b.pre_run_wait("r3", should_stop=lambda: clock.t >= 2000.0) is False
    # Canary success closes the breaker and clears every runner's backoff.
    assert b.record("r2", ok=True, duration_sec=42.0) == "write"
    assert not b.is_open
    assert any("BREAKER CLOSED" in m for m in logs)
    assert 900.0 <= b.open_time() <= 2000.0
    before = len(clock.sleeps)
    assert b.pre_run_wait("r1") is False       # r1's 10 fails forgotten
    assert clock.sleeps[before:] == []


def test_row_suppression_while_open_caps_rows_by_construction():
    clock = _Clock()
    b, _ = _breaker(clock)
    _fail_instant(b, "r1", n=10)               # opens; these rows written
    # While open, the first canary failures still write rows, up to the
    # per-episode cap (the opening row already consumed 1 of it)...
    actions = []
    for _ in range(6):
        clock.t += 900.0                       # one canary per interval
        actions.append(b.record("r1", ok=False, duration_sec=0.02))
    assert actions[:4] == ["write"] * 4        # cap 5 = 1 opening + 4 more
    # ...then each interval gets ONE summary row, never a row per failure.
    assert actions[4:] == ["summary", "summary"]
    # A second failure inside the SAME interval is suppressed outright.
    assert b.record("r1", ok=False, duration_sec=0.02) == "suppress"
    assert b.suppressed_rows == 3
    assert b.total_failures == 17
    assert b.is_open


def test_storm_counters_survive_close():
    clock = _Clock()
    b, _ = _breaker(clock)
    _fail_instant(b, "r1", n=10)
    clock.t += 900.0
    assert b.pre_run_wait("r1") is True        # claim the canary
    b.record("r1", ok=True, duration_sec=30.0)
    assert b.total_failures == 10
    assert b.open_count == 1
    assert not b.is_open
    t = b.open_time()
    assert t >= 900.0
    clock.t += 500.0
    assert b.open_time() == t                  # frozen while closed


def test_record_gauntlet_suppress_action_writes_no_row(tmp_path):
    clock = _Clock()
    s = _soak_stub(tmp_path)
    s.breaker, _ = _breaker(clock)
    res = GauntletResult(
        test_deck="[USER] D [B3].dck", games=0, wins=0, losses=0,
        status="failed", error="Forge exited with code 3221225794",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck", action="suppress")
    assert s.sims_failed == 1                  # counters stay accurate
    assert _read_rows(s.args.out) == []        # but no row hits disk


def test_record_gauntlet_summary_action_writes_one_storm_row(tmp_path):
    clock = _Clock()
    s = _soak_stub(tmp_path)
    s.breaker, _ = _breaker(clock)
    s.breaker.suppressed_rows = 84_950_245     # plain counter; set directly
    s._write_storm_summary_locked = (
        lambda: soak_pool.Soak._write_storm_summary_locked(s))
    res = GauntletResult(
        test_deck="[USER] D [B3].dck", games=0, wins=0, losses=0,
        status="failed", error="Forge exited with code 3221225794",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck", action="summary")
    assert s.sims_failed == 1
    (row,) = _read_rows(s.args.out)
    # Never status='done'/'loop_unattributed', so merge_soak's fold and
    # the gauntlet analyzers skip it.
    assert row["status"] == "storm_suppressed"
    assert row["suppressed_failures"] == 84_950_245
    assert "breaker_open_sec" in row


def test_record_ab_suppress_action_writes_no_row(tmp_path):
    clock = _Clock()
    s = _soak_stub(tmp_path)
    s.breaker, _ = _breaker(clock)
    soak_pool.Soak._record(
        s, None, "OSError: launch failed", tmp_path / "a.dck",
        tmp_path / "b.dck", action="suppress")
    assert s.sims_failed == 1
    assert _read_rows(s.args.out) == []


def test_write_summary_includes_storm_counters(tmp_path):
    clock = _Clock()
    s = _soak_stub(tmp_path)
    s.start_t = 0.0
    s.mode = "gauntlet"
    s.phase = 1
    s.current_games = 12
    s.last_cpu = 50.0
    s.max = 4
    s.active_count = lambda: 2
    s.args.hours = 24.0
    s.args.games = 12
    s.args.phase2_games = 40
    s.args.phase2_after = 200
    s.args.min = 2
    s.args.summary = tmp_path / "summary.json"
    s.breaker, _ = _breaker(clock)
    _fail_instant(s.breaker, "r1", n=10)
    soak_pool.Soak.write_summary(s)
    storm = json.loads(s.args.summary.read_text(encoding="utf-8"))["storm"]
    assert storm["total_failures"] == 10
    assert storm["suppressed_rows"] == 0
    assert storm["breaker_open"] is True
    assert storm["breaker_opens"] == 1
    assert storm["breaker_open_hours"] == 0.0  # just opened on a frozen clock


# --- pair discovery: [USER] and [PREMADE] both feed FP-002 ----------------

def _touch_decks(deck_dir: Path, names: list[str]) -> None:
    deck_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (deck_dir / n).write_text("[Commander]\n1 X\n", encoding="utf-8")


def test_deck_pairs_finds_premade_pairs_alongside_user_pairs(tmp_path):
    _touch_decks(tmp_path, [
        "[USER] Mine [B3].dck", "[USER] Mine v2 [B3].dck",
        "[PREMADE] Popular [B4].dck", "[PREMADE] Popular v2 [B4].dck",
    ])
    pairs = [(a.name, b.name) for a, b in soak_pool._deck_pairs(tmp_path)]
    assert ("[USER] Mine [B3].dck", "[USER] Mine v2 [B3].dck") in pairs
    assert ("[PREMADE] Popular [B4].dck",
            "[PREMADE] Popular v2 [B4].dck") in pairs
    assert len(pairs) == 2


def test_deck_pairs_user_only_behavior_unchanged(tmp_path):
    # A library with no premades yields exactly the historical selection.
    _touch_decks(tmp_path, [
        "[USER] Mine [B3].dck", "[USER] Mine v2 [B3].dck",
        "[USER] Unpaired v2 [B2].dck",       # v2 with no base: excluded
        "Pool Deck [B3].dck", "Pool Deck v2 [B3].dck",  # pool role: excluded
    ])
    pairs = [(a.name, b.name) for a, b in soak_pool._deck_pairs(tmp_path)]
    assert pairs == [("[USER] Mine [B3].dck", "[USER] Mine v2 [B3].dck")]


def test_deck_pairs_premade_v2_without_base_is_excluded(tmp_path):
    _touch_decks(tmp_path, ["[PREMADE] Orphan v2 [B3].dck"])
    assert soak_pool._deck_pairs(tmp_path) == []


def test_record_gauntlet_premade_row_schema_unchanged(tmp_path):
    # pair_base/test_deck simply carry premade filenames; role derivation
    # (' v2 ' token) is prefix-agnostic.
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[PREMADE] Popular v2 [B4].dck",
        gauntlet=["G1.dck", "G2.dck", "G3.dck"],
        games=12, wins=4, losses=8, draws=0, status="done", error=None,
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[PREMADE] Popular v2 [B4].dck")
    (row,) = _read_rows(s.args.out)
    assert row["role"] == "v2"
    assert row["pair_base"] == "[PREMADE] Popular [B4].dck"
    assert row["test_deck"] == "[PREMADE] Popular v2 [B4].dck"
