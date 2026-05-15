"""forge_runner tests — focused on the streaming/non-streaming dispatch.

The actual Forge subprocess can't be unit-tested without a Forge install, so
we mock at the subprocess boundary. The blocking path was already exercised
by every live integration script in `scripts/`; this file pins the new
streaming code (GAP-008) so it doesn't drift.
"""
from unittest.mock import MagicMock, patch

import pytest

from commander_builder.forge_runner import (
    ForgeVersionInfo,
    SimResult,
    _run_blocking,
    _run_streaming,
    detect_forge_version,
)


def test_run_blocking_returns_captured_streams(monkeypatch):
    fake_proc = MagicMock(stdout="match output\n", stderr="", returncode=0)
    with patch("commander_builder.forge_runner.subprocess.run", return_value=fake_proc):
        stdout, stderr, rc, timed_out, error = _run_blocking(
            ["fake"], timeout=10, cwd="/tmp",
        )
    assert stdout == "match output\n"
    assert rc == 0
    assert not timed_out
    assert error is None


def test_run_blocking_handles_timeout(monkeypatch):
    import subprocess
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=10, output="partial", stderr="")
    monkeypatch.setattr("commander_builder.forge_runner.subprocess.run", raise_timeout)
    stdout, stderr, rc, timed_out, error = _run_blocking(
        ["fake"], timeout=10, cwd="/tmp",
    )
    assert timed_out is True
    assert stdout == "partial"
    assert "Timed out" in error


def test_run_streaming_drains_stdout_via_callback(monkeypatch):
    """The on_line callback should fire for every line as it arrives."""
    captured: list[str] = []

    fake_proc = MagicMock()
    fake_proc.stdout = iter(["line one\n", "line two\n", "line three\n"])
    fake_proc.stderr = iter([])
    fake_proc.wait = MagicMock(return_value=0)

    with patch("commander_builder.forge_runner.subprocess.Popen", return_value=fake_proc):
        stdout, stderr, rc, timed_out, error = _run_streaming(
            ["fake"], timeout=10, cwd="/tmp",
            stream=False, on_line=lambda s: captured.append(s),
        )
    assert captured == ["line one", "line two", "line three"]
    assert stdout == "line one\nline two\nline three\n"
    assert rc == 0
    assert not timed_out


def test_run_streaming_swallows_callback_exceptions(monkeypatch):
    """A buggy on_line shouldn't take down the whole sim — the callback runs
    inside a try/except in the consumer thread."""
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["line one\n", "line two\n"])
    fake_proc.stderr = iter([])
    fake_proc.wait = MagicMock(return_value=0)

    def crash(_): raise RuntimeError("boom")

    with patch("commander_builder.forge_runner.subprocess.Popen", return_value=fake_proc):
        stdout, _, rc, _, _ = _run_streaming(
            ["fake"], timeout=10, cwd="/tmp",
            stream=False, on_line=crash,
        )
    # Sim still completed, output captured.
    assert "line one" in stdout
    assert rc == 0


def test_run_streaming_handles_timeout(monkeypatch):
    import subprocess
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["partial\n"])
    fake_proc.stderr = iter([])
    fake_proc.wait = MagicMock(side_effect=[
        subprocess.TimeoutExpired(cmd="fake", timeout=10), 0,
    ])
    fake_proc.kill = MagicMock()

    with patch("commander_builder.forge_runner.subprocess.Popen", return_value=fake_proc):
        stdout, stderr, rc, timed_out, error = _run_streaming(
            ["fake"], timeout=10, cwd="/tmp", stream=False,
        )
    assert timed_out is True
    assert "Timed out" in error
    fake_proc.kill.assert_called_once()


def test_run_streaming_handles_popen_failure(monkeypatch):
    """If Popen itself fails (e.g. java binary missing), return a clean error
    rather than crashing."""
    with patch(
        "commander_builder.forge_runner.subprocess.Popen",
        side_effect=FileNotFoundError("java not found"),
    ):
        stdout, stderr, rc, timed_out, error = _run_streaming(
            ["fake"], timeout=10, cwd="/tmp", stream=False,
        )
    assert rc is None
    assert error is not None
    assert "java not found" in error


# --- SimResult sanity ------------------------------------------------------

def test_sim_result_to_dict_includes_streaming_metadata():
    r = SimResult(
        cmd=["x"], returncode=0, duration_sec=1.5,
        stdout="ok", stderr="", timed_out=False, error=None,
    )
    d = r.to_dict()
    assert d["returncode"] == 0
    assert d["duration_sec"] == 1.5
    assert d["timed_out"] is False


# --- detect_forge_version — startup staleness check ------------------------

def _write_fake_forge(tmp_path, jar_name: str, build_text: str | None = None):
    """Create a fake vendor/forge/ layout: a jar with the given name and an
    optional build.txt."""
    (tmp_path / jar_name).write_bytes(b"PK\x03\x04")  # zip header — content irrelevant
    if build_text is not None:
        (tmp_path / "build.txt").write_text(build_text, encoding="utf-8")
    return tmp_path


def test_detect_forge_version_parses_version_from_filename(tmp_path):
    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text="2026-04-23 19:50:58",
    )
    info = detect_forge_version(tmp_path)
    assert info.version == "2.0.12"
    assert info.jar_path is not None
    assert info.jar_path.name == "forge-gui-desktop-2.0.12-jar-with-dependencies.jar"


def test_detect_forge_version_reads_build_date(tmp_path):
    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text="2026-04-23 19:50:58",
    )
    info = detect_forge_version(tmp_path)
    assert info.build_date is not None
    assert info.build_date.year == 2026
    assert info.build_date.month == 4
    assert info.build_date.day == 23


def test_detect_forge_version_computes_age_days(tmp_path, monkeypatch):
    """age_days is computed from build_date relative to now()."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text="2026-02-01 00:00:00",  # ~100 days before May 12
    )

    # Pin "now" to a known value so the test is deterministic.
    class _FixedNow:
        @staticmethod
        def now(tz=None):
            return _dt(2026, 5, 12, 0, 0, 0, tzinfo=_tz.utc)
    monkeypatch.setattr(
        "commander_builder.forge_runner._utcnow", _FixedNow.now,
    )

    info = detect_forge_version(tmp_path)
    assert info.age_days is not None
    # Feb 1 → May 12 = 100 days.
    assert 99 <= info.age_days <= 101
    assert info.is_stale is True  # > 90 days


def test_detect_forge_version_not_stale_when_recent(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text="2026-04-23 19:50:58",
    )
    monkeypatch.setattr(
        "commander_builder.forge_runner._utcnow",
        lambda tz=None: _dt(2026, 5, 12, 0, 0, 0, tzinfo=_tz.utc),
    )
    info = detect_forge_version(tmp_path)
    assert info.age_days is not None
    assert info.age_days < 90
    assert info.is_stale is False


def test_detect_forge_version_missing_jar(tmp_path):
    """No jar in dir → version=None, jar_path=None, is_stale=False."""
    info = detect_forge_version(tmp_path)
    assert info.version is None
    assert info.jar_path is None
    assert info.build_date is None
    assert info.is_stale is False


def test_detect_forge_version_missing_build_txt(tmp_path):
    """Jar exists but no build.txt → version parsed, build_date=None,
    is_stale=False (can't determine, don't alarm)."""
    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text=None,
    )
    info = detect_forge_version(tmp_path)
    assert info.version == "2.0.12"
    assert info.build_date is None
    assert info.age_days is None
    assert info.is_stale is False


def test_detect_forge_version_malformed_build_txt(tmp_path):
    """Garbage build.txt content → no crash, build_date=None."""
    _write_fake_forge(
        tmp_path,
        "forge-gui-desktop-2.0.12-jar-with-dependencies.jar",
        build_text="not a date",
    )
    info = detect_forge_version(tmp_path)
    assert info.version == "2.0.12"
    assert info.build_date is None
    assert info.is_stale is False


def test_detect_forge_version_returns_dataclass(tmp_path):
    """ForgeVersionInfo is a dataclass so tests / endpoints can asdict() it."""
    from dataclasses import is_dataclass
    info = detect_forge_version(tmp_path)
    assert isinstance(info, ForgeVersionInfo)
    assert is_dataclass(info)


def test_detect_forge_version_picks_newest_when_multiple_jars_present(tmp_path):
    """Regression: lexicographic sort puts "2.0.10" before "2.0.12"
    because "0" < "2" at the relevant position, so the previous
    sorted(...)[0] picked the *older* jar when a user kept both
    around after an upgrade. The selector must rank by parsed
    semver-ish version, not by alphabetical filename.
    """
    (tmp_path / "forge-gui-desktop-2.0.10-jar-with-dependencies.jar").write_bytes(b"PK")
    (tmp_path / "forge-gui-desktop-2.0.12-jar-with-dependencies.jar").write_bytes(b"PK")
    (tmp_path / "forge-gui-desktop-2.0.9-jar-with-dependencies.jar").write_bytes(b"PK")

    info = detect_forge_version(tmp_path)
    assert info.version == "2.0.12"
    assert "2.0.12" in info.jar_path.name


def test_detect_forge_version_double_digit_minor_sorts_correctly(tmp_path):
    """Same class of bug: 2.0 vs 2.10 — alphabetical sort says
    "2.0" < "2.10" but semver says the opposite."""
    (tmp_path / "forge-gui-desktop-2.0-jar-with-dependencies.jar").write_bytes(b"PK")
    (tmp_path / "forge-gui-desktop-2.10-jar-with-dependencies.jar").write_bytes(b"PK")

    info = detect_forge_version(tmp_path)
    assert info.version == "2.10"


def test_detect_forge_version_prefers_fat_jar_over_thin(tmp_path):
    """When both 'jar-with-dependencies' and a plain jar of the
    *same* version exist, the fat jar must win (it's what
    forge_runner.locate() runs). Pre-existing behavior — pin it."""
    (tmp_path / "forge-gui-desktop-2.0.12.jar").write_bytes(b"PK")
    (tmp_path / "forge-gui-desktop-2.0.12-jar-with-dependencies.jar").write_bytes(b"PK")

    info = detect_forge_version(tmp_path)
    assert "jar-with-dependencies" in info.jar_path.name


def test_detect_forge_version_unparseable_filename_is_skipped(tmp_path):
    """A stray jar that doesn't match the version regex shouldn't poison
    the selection — pick the highest *parseable* version instead."""
    (tmp_path / "forge-gui-desktop-CUSTOM-jar-with-dependencies.jar").write_bytes(b"PK")
    (tmp_path / "forge-gui-desktop-2.0.12-jar-with-dependencies.jar").write_bytes(b"PK")

    info = detect_forge_version(tmp_path)
    assert info.version == "2.0.12"


# --- run_ab_simulation — head-to-head old-vs-new A/B harness ----------------


def _ab_canned_stdout(end_turn: int, winner_seat: int, winner_name: str,
                      seats: list[str]) -> str:
    """Synthesize a Forge sim stdout payload for one game.

    Lines emitted MUST match the live Forge regex shapes consumed by
    ``log_parser`` and ``game_analyzer``:

    - ``Turn: Turn N (Ai(M)-DeckName)`` — seeds per-seat deck identities
      so the analyzer can attribute wins/turns to the right deck.
    - ``Game Outcome: Turn N`` — authoritative end-turn marker that
      overrides any inferred turn count.
    - ``Game Result: Game N ended in NNNN ms. Ai(M)-Winner has won!``
      — terminates the buffered game and carries the winner seat+name.
    - ``Match Result: Ai(1)-A: wins ...`` — cumulative per-deck wins,
      parsed by log_parser to attribute to ``deck_results``.
    """
    # One Turn line per seat seeds deck identities for the analyzer.
    turn_seeds = "\n".join(
        f"Turn: Turn 1 (Ai({i + 1})-{n})"
        for i, n in enumerate(seats)
    )
    match_parts = " ".join(
        f"Ai({i + 1})-{n}: {1 if (i + 1) == winner_seat else 0}"
        for i, n in enumerate(seats)
    )
    return (
        f"{turn_seeds}\n"
        f"Game Outcome: Turn {end_turn}\n"
        f"Game Result: Game 1 ended in 540000 ms. "
        f"Ai({winner_seat})-{winner_name} has won!\n"
        f"Match Result: {match_parts}\n"
    )


def test_run_ab_simulation_alternates_seat_order_per_game(tmp_path):
    """Game 1 puts Deck A in seat 1; game 2 puts Deck B in seat 1;
    alternating balances first-player advantage. The function must
    drive the runner with the right ``deck_filenames`` list per
    iteration."""
    from commander_builder.forge_runner import run_ab_simulation

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n1 Sol Ring\n", encoding="utf-8")
    deck_b.write_text("[Main]\n1 Sol Ring\n", encoding="utf-8")

    captured_orders: list[list[str]] = []

    class _FakeRunner:
        def run(self, deck_filenames, num_games, **kwargs):
            captured_orders.append(list(deck_filenames))
            return SimResult(
                cmd=["fake"], returncode=0, duration_sec=1.0,
                stdout=_ab_canned_stdout(
                    10, 3, "filler1",
                    seats=["DeckA", "DeckB", "filler1", "filler2"],
                ),
                stderr="", timed_out=False, error=None,
            )

    result = run_ab_simulation(
        deck_a, deck_b, games=4,
        runner=_FakeRunner(),
        fillers=["filler1.dck", "filler2.dck"],
    )

    assert result.games == 4
    assert len(captured_orders) == 4
    # Games 0, 2: Deck A in seat 1.
    assert captured_orders[0][0] == deck_a.name
    assert captured_orders[0][1] == deck_b.name
    assert captured_orders[2][0] == deck_a.name
    # Games 1, 3: Deck B in seat 1.
    assert captured_orders[1][0] == deck_b.name
    assert captured_orders[1][1] == deck_a.name
    assert captured_orders[3][0] == deck_b.name


def test_run_ab_simulation_records_wins_and_turn_stats(tmp_path):
    """Wins are attributed by deck identity, not seat. Average
    turns-when-won is computed only over games each deck won."""
    from commander_builder.forge_runner import run_ab_simulation

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    # 4 sims; seat order alternates A-first / B-first.
    canned = [
        # Game 0: seats = [DeckA, DeckB, f1, f2], A wins turn 12.
        _ab_canned_stdout(12, 1, "DeckA",
                          seats=["DeckA", "DeckB", "filler1", "filler2"]),
        # Game 1: seats = [DeckB, DeckA, f1, f2], B wins turn 8.
        _ab_canned_stdout(8, 1, "DeckB",
                          seats=["DeckB", "DeckA", "filler1", "filler2"]),
        # Game 2: seats = [DeckA, DeckB, ...], A wins turn 14.
        _ab_canned_stdout(14, 1, "DeckA",
                          seats=["DeckA", "DeckB", "filler1", "filler2"]),
        # Game 3: seats = [DeckB, DeckA, ...], A wins from seat 2, turn 10.
        _ab_canned_stdout(10, 2, "DeckA",
                          seats=["DeckB", "DeckA", "filler1", "filler2"]),
    ]

    class _FakeRunner:
        def __init__(self):
            self.idx = 0

        def run(self, deck_filenames, num_games, **kwargs):
            stdout = canned[self.idx]
            self.idx += 1
            return SimResult(
                cmd=["fake"], returncode=0, duration_sec=1.0,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    result = run_ab_simulation(
        deck_a, deck_b, games=4,
        runner=_FakeRunner(),
        fillers=["filler1.dck", "filler2.dck"],
    )

    assert result.status == "done"
    assert result.games == 4
    # A won games 0, 2, 3 → 3 wins. B won game 1 → 1 win.
    assert result.wins_a == 3
    assert result.wins_b == 1
    # Avg turns when A won: (12+14+10)/3 = 12.0
    # Avg turns when B won: 8.0
    assert result.avg_turns_a == pytest.approx(12.0, abs=0.5)
    assert result.avg_turns_b == pytest.approx(8.0, abs=0.5)


def test_run_ab_simulation_skips_when_forge_not_installed(tmp_path, monkeypatch):
    """When ForgeRunner.locate() raises (no JRE / no vendor/forge), the
    helper returns status='skipped' with the error captured rather
    than propagating. Lets the background queue log the skip without
    taking the save-iteration HTTP response down."""
    from commander_builder.forge_runner import run_ab_simulation, ForgeRunner

    def _raise(cls):
        raise FileNotFoundError("Forge jar not found")

    monkeypatch.setattr(ForgeRunner, "locate", classmethod(_raise))

    deck_a = tmp_path / "a.dck"
    deck_b = tmp_path / "b.dck"
    deck_a.write_text("", encoding="utf-8")
    deck_b.write_text("", encoding="utf-8")

    result = run_ab_simulation(deck_a, deck_b, games=5)

    assert result.status == "skipped"
    assert result.games == 0
    assert "Forge" in (result.error or "")
    assert result.wins_a == 0
    assert result.wins_b == 0


def test_run_ab_simulation_captures_failure_from_runner(tmp_path):
    """Non-zero exit / runner error becomes status='failed' so the UI
    banner can show 'Sim failed — see logs' instead of silently
    showing 0-0 'done'."""
    from commander_builder.forge_runner import run_ab_simulation

    deck_a = tmp_path / "a.dck"
    deck_b = tmp_path / "b.dck"
    deck_a.write_text("", encoding="utf-8")
    deck_b.write_text("", encoding="utf-8")

    class _BrokenRunner:
        def run(self, deck_filenames, num_games, **kwargs):
            return SimResult(
                cmd=["fake"], returncode=1, duration_sec=0.1,
                stdout="", stderr="java crashed",
                timed_out=False, error="JVM exited unexpectedly",
            )

    result = run_ab_simulation(
        deck_a, deck_b, games=3,
        runner=_BrokenRunner(),
        fillers=["f1.dck", "f2.dck"],
    )

    assert result.status == "failed"
    assert result.error is not None


def test_run_ab_simulation_requires_two_fillers(tmp_path):
    """Commander format demands a 4-player pod; <2 fillers is a skip,
    not a crash. The runner must NOT be called."""
    from commander_builder.forge_runner import run_ab_simulation

    deck_a = tmp_path / "a.dck"
    deck_b = tmp_path / "b.dck"
    deck_a.write_text("", encoding="utf-8")
    deck_b.write_text("", encoding="utf-8")

    class _NeverCalledRunner:
        def run(self, *args, **kwargs):
            raise AssertionError("should not be invoked")

    result = run_ab_simulation(
        deck_a, deck_b, games=5,
        runner=_NeverCalledRunner(),
        fillers=["only_one.dck"],
    )
    assert result.status == "skipped"
    assert "filler" in (result.error or "").lower()


def test_ab_result_to_dict_is_json_safe():
    """ABResult round-trips through dict→json so the iteration row
    can persist it without bespoke serialization."""
    import json
    from commander_builder.forge_runner import ABResult
    r = ABResult(
        deck_a="a.dck", deck_b="b.dck",
        wins_a=3, wins_b=2, games=5,
        avg_turns_a=11.0, avg_turns_b=13.5,
        status="done",
    )
    d = r.to_dict()
    blob = json.dumps(d)
    parsed = json.loads(blob)
    assert parsed["wins_a"] == 3
    assert parsed["wins_b"] == 2
    assert parsed["games"] == 5
    assert parsed["status"] == "done"
