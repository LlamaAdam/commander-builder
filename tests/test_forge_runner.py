"""forge_runner tests — focused on the streaming/non-streaming dispatch.

The actual Forge subprocess can't be unit-tested without a Forge install, so
we mock at the subprocess boundary. The blocking path was already exercised
by every live integration script in `scripts/`; this file pins the new
streaming code (GAP-008) so it doesn't drift.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commander_builder.forge_runner import (
    ForgeVersionInfo,
    SimResult,
    _run_blocking,
    _run_streaming,
    coerce_output_text,
    detect_forge_version,
    scrubbed_child_env,
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


def test_run_blocking_timeout_with_bytes_stdout_yields_str(monkeypatch):
    """TimeoutExpired.stdout is BYTES on POSIX even when encoding= was set
    (CPython re-raises the raw pipe contents; the text decode only happens
    on the CompletedProcess path). SimResult.stdout flows into log_parser's
    regexes which require str — so the handler must decode defensively."""
    import subprocess
    def raise_timeout(*a, **kw):
        # \xf0 alone is invalid UTF-8 — exercises errors="replace" too.
        raise subprocess.TimeoutExpired(
            cmd="fake", timeout=10, output=b"partial \xf0 bytes", stderr=b"e",
        )
    monkeypatch.setattr("commander_builder.forge_runner.subprocess.run", raise_timeout)
    stdout, stderr, rc, timed_out, error = _run_blocking(
        ["fake"], timeout=10, cwd="/tmp",
    )
    assert timed_out is True
    assert isinstance(stdout, str) and isinstance(stderr, str)
    assert "partial" in stdout and "�" in stdout
    assert stderr == "e"


def test_coerce_output_text_normalizes_all_shapes():
    assert coerce_output_text(None) == ""
    assert coerce_output_text("already str") == "already str"
    assert coerce_output_text(b"ok bytes") == "ok bytes"
    # Invalid UTF-8 degrades to U+FFFD instead of raising.
    assert coerce_output_text(b"bad \xf0 byte") == "bad � byte"


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


# --- Anthropic credential scrubbing (2026-07-19) --------------------------
#
# _secrets.load_credentials() exports ANTHROPIC_API_KEY into os.environ, and
# subprocesses inherit the parent env by default — so without an explicit
# env= scrub every Forge JVM would silently hold a live Anthropic credential.
# These tests pin the scrub on both launch paths.

def test_scrubbed_child_env_drops_anthropic_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-inherit")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-inherit")
    monkeypatch.setenv("SOME_HARMLESS_VAR", "keep-me")
    env = scrubbed_child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # Everything else is inherited — Forge needs PATH/JAVA_HOME/etc.
    assert env["SOME_HARMLESS_VAR"] == "keep-me"


def test_run_blocking_env_excludes_anthropic_key(monkeypatch):
    """The blocking Forge launch must pass an env= that excludes
    ANTHROPIC_API_KEY even when the parent process holds one."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-jvm")
    fake_proc = MagicMock(stdout="", stderr="", returncode=0)
    with patch(
        "commander_builder.forge_runner.subprocess.run",
        return_value=fake_proc,
    ) as run_mock:
        _run_blocking(["fake-java"], timeout=10, cwd="/tmp")
    env = run_mock.call_args.kwargs.get("env")
    assert env is not None, "Forge launch must pass an explicit env="
    assert "ANTHROPIC_API_KEY" not in env


def test_run_streaming_env_excludes_anthropic_key(monkeypatch):
    """Same guarantee on the streaming (Popen) Forge launch path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-jvm")
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["line\n"])
    fake_proc.stderr = iter([])
    fake_proc.wait = MagicMock(return_value=0)
    with patch(
        "commander_builder.forge_runner.subprocess.Popen",
        return_value=fake_proc,
    ) as popen_mock:
        _run_streaming(["fake-java"], timeout=10, cwd="/tmp", stream=False)
    env = popen_mock.call_args.kwargs.get("env")
    assert env is not None, "Forge launch must pass an explicit env="
    assert "ANTHROPIC_API_KEY" not in env


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
    """Wins are attributed by seat (which run_ab_simulation controls per
    game), so they're correct even when A and B share an internal name.
    Average turns-when-won is computed only over games each deck won."""
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


def test_run_ab_simulation_attributes_by_seat_when_names_collide(tmp_path):
    """Regression: deck A and deck B often share the same internal `Name=`
    (a curated deck keeps its parent's Name=; a detuned deck keeps the
    original's). Forge then emits identical "Ai(N)-<Name>" tokens for both,
    so the OLD name-based attribution matched neither stem-key and recorded
    0-0 (every verdict forced to neutral) — or funnelled all wins to one
    side, fabricating kept/reverted. Seat-based attribution must credit the
    real winner. Here B wins both games despite both decks being "Twin"."""
    from commander_builder.forge_runner import run_ab_simulation

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    # Both head-to-head seats report the identical Forge name "Twin".
    canned = [
        # Game 0: order [DeckA, DeckB, f1, f2] -> B is seat 2; B wins.
        _ab_canned_stdout(11, 2, "Twin",
                          seats=["Twin", "Twin", "filler1", "filler2"]),
        # Game 1: order [DeckB, DeckA, f1, f2] -> B is seat 1; B wins.
        _ab_canned_stdout(9, 1, "Twin",
                          seats=["Twin", "Twin", "filler1", "filler2"]),
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
        deck_a, deck_b, games=2,
        runner=_FakeRunner(),
        fillers=["filler1.dck", "filler2.dck"],
    )

    assert result.status == "done"
    # B won both games; seat attribution must credit B, not silently 0-0.
    assert result.wins_b == 2
    assert result.wins_a == 0
    # Turn stats follow the same seat path.
    assert result.avg_turns_b == pytest.approx(10.0, abs=0.5)


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
    # Draw-policy label (2026-07-19): the A/B harness resolves turn-cap
    # draws to the surviving life leader; downstream analysis reads this
    # to distinguish AB-shaped reports from plain-draw compare shapes.
    assert parsed["draw_policy"] == "resolve_survivor_leader"


def test_gauntlet_result_carries_draw_policy_label():
    from dataclasses import asdict
    from commander_builder.forge_runner import GauntletResult
    d = asdict(GauntletResult(test_deck="t.dck"))
    assert d["draw_policy"] == "resolve_survivor_leader"


# --- run_ab_batch — concurrent A/B sims across a pool of profiles (FP-003) --


def _ab_done(deck_a, deck_b, *, games, runner, fillers, game_format):
    """Stand-in for run_ab_simulation: returns a 'done' ABResult tagged with
    which runner serviced it, so tests can assert pool assignment."""
    from commander_builder.forge_runner import ABResult
    return ABResult(
        deck_a=Path(deck_a).name, deck_b=Path(deck_b).name,
        wins_a=games, wins_b=0, games=games, status="done",
        error=getattr(runner, "tag", None),  # stash runner identity for asserts
    )


def test_run_ab_batch_runs_all_jobs_in_order():
    """Results come back aligned to the jobs list (not completion order)."""
    from commander_builder.forge_runner import run_ab_batch, ABJob

    jobs = [
        ABJob(deck_a=Path(f"d{i}a.dck"), deck_b=Path(f"d{i}b.dck"),
              fillers=["f1.dck", "f2.dck"], games=i + 1)
        for i in range(5)
    ]

    class _R:
        tag = "r"

    results = run_ab_batch(jobs, [_R(), _R()], _sim_fn=_ab_done)
    assert len(results) == 5
    # games echoes the per-job override, in order → proves order preservation.
    assert [r.games for r in results] == [1, 2, 3, 4, 5]
    assert all(r.status == "done" for r in results)
    assert [r.deck_a for r in results] == [f"d{i}a.dck" for i in range(5)]


def test_run_ab_batch_never_shares_a_runner_concurrently():
    """The whole point of the pool: two jobs must never run on the same
    profile at once (they'd collide on deck dir / cache / forge.log).
    Caps global concurrency at len(runners)."""
    import threading
    import time
    from commander_builder.forge_runner import run_ab_batch, ABJob, ABResult

    lock = threading.Lock()
    per_runner_active: dict[int, int] = {}
    max_global = {"v": 0}
    active = {"v": 0}

    def _slow_sim(deck_a, deck_b, *, games, runner, fillers, game_format):
        rid = id(runner)
        with lock:
            per_runner_active[rid] = per_runner_active.get(rid, 0) + 1
            active["v"] += 1
            # invariants: no runner double-booked; global cap respected.
            assert per_runner_active[rid] == 1, "runner serviced two jobs at once"
            max_global["v"] = max(max_global["v"], active["v"])
        time.sleep(0.05)
        with lock:
            per_runner_active[rid] -= 1
            active["v"] -= 1
        return ABResult(deck_a=deck_a.name, deck_b=deck_b.name,
                        games=games, status="done")

    runners = [object(), object()]  # 2 distinct profiles
    jobs = [ABJob(deck_a=Path(f"{i}.dck"), deck_b=Path(f"{i}b.dck"),
                  fillers=["f1.dck", "f2.dck"]) for i in range(8)]

    results = run_ab_batch(jobs, runners, _sim_fn=_slow_sim)
    assert len(results) == 8
    assert all(r.status == "done" for r in results)
    # With 2 profiles and 8 quick jobs, both should run together at least once.
    assert max_global["v"] == 2


def test_run_ab_batch_empty_jobs_returns_empty():
    from commander_builder.forge_runner import run_ab_batch
    assert run_ab_batch([], [object()], _sim_fn=_ab_done) == []


def test_run_ab_batch_requires_a_runner():
    from commander_builder.forge_runner import run_ab_batch, ABJob
    with pytest.raises(ValueError):
        run_ab_batch([ABJob(deck_a=Path("a.dck"), deck_b=Path("b.dck"))],
                     [], _sim_fn=_ab_done)


def test_run_ab_batch_passes_per_job_overrides():
    """Per-job games/game_format override the batch defaults."""
    from commander_builder.forge_runner import run_ab_batch, ABJob, ABResult

    seen: list[tuple[int, str]] = []

    def _capture(deck_a, deck_b, *, games, runner, fillers, game_format):
        seen.append((games, game_format))
        return ABResult(status="done")

    jobs = [
        ABJob(deck_a=Path("a.dck"), deck_b=Path("b.dck")),  # uses defaults
        ABJob(deck_a=Path("c.dck"), deck_b=Path("d.dck"),
              games=9, game_format="constructed"),          # overrides
    ]
    run_ab_batch(jobs, [object()], games=5, game_format="commander",
                 _sim_fn=_capture)
    assert (5, "commander") in seen
    assert (9, "constructed") in seen


def test_for_profile_shares_jar_but_distinct_cwd(tmp_path, monkeypatch):
    """ForgeRunner.for_profile reuses the located java + jar (shared across
    profiles) but swaps the cwd to the requested profile dir."""
    from commander_builder.forge_runner import ForgeRunner

    base = ForgeRunner(java_path=Path("/j/java"), forge_jar=Path("/f/forge.jar"),
                       forge_dir=Path("/f"))
    monkeypatch.setattr(ForgeRunner, "locate", classmethod(lambda cls: base))

    r2 = ForgeRunner.for_profile(tmp_path / "forge2")
    assert r2.java_path == base.java_path
    assert r2.forge_jar == base.forge_jar
    assert r2.forge_dir == tmp_path / "forge2"


def test_read_forge_log_tail_prefers_userdata(tmp_path):
    """Forge writes its log under userDir (userdata/forge.log); the tail
    reader must look there, not just the program-dir root."""
    from commander_builder.forge_runner import ForgeRunner
    (tmp_path / "userdata").mkdir()
    (tmp_path / "userdata" / "forge.log").write_text("under userdata\n",
                                                     encoding="utf-8")
    runner = ForgeRunner(java_path=Path("j"), forge_jar=Path("f"),
                         forge_dir=tmp_path)
    assert "under userdata" in runner._read_forge_log_tail()


def test_read_forge_log_tail_falls_back_to_root(tmp_path):
    """If a profile left userDir at default, forge.log may sit at the root —
    still found."""
    from commander_builder.forge_runner import ForgeRunner
    (tmp_path / "forge.log").write_text("at root\n", encoding="utf-8")
    runner = ForgeRunner(java_path=Path("j"), forge_jar=Path("f"),
                         forge_dir=tmp_path)
    assert "at root" in runner._read_forge_log_tail()


def test_read_forge_log_tail_missing_returns_empty(tmp_path):
    from commander_builder.forge_runner import ForgeRunner
    runner = ForgeRunner(java_path=Path("j"), forge_jar=Path("f"),
                         forge_dir=tmp_path)
    assert runner._read_forge_log_tail() == ""


# --- Per-game timeout salvage (operator verdict-scoring policy point 2) -----
#
# A single game that hits the per-game wall timeout (combo loop / hang) no
# longer fails the whole batch. Games already tallied are kept; the timed-out
# game is credited to the SEAT whose turn it was in the LAST "Turn:" line (the
# looping/active player), then status=done. Only sim.timed_out triggers this —
# a genuine non-zero exit / non-timeout error still fails the batch.


def _timeout_sim(stdout: str) -> SimResult:
    """A SimResult shaped like a per-game wall timeout: timed_out=True, no
    returncode, and the 'Timed out after Ns' error from the runner path."""
    return SimResult(
        cmd=["fake"], returncode=None, duration_sec=180.0,
        stdout=stdout, stderr="", timed_out=True,
        error="Timed out after 180s",
    )


def _in_progress_turns(active_seat: int, name: str = "Loop") -> str:
    """A partial game stream that loops: the last Turn line names ``active_seat``
    as the active player and the game never produces a Game Result."""
    return (
        "Turn: Turn 1 (Ai(1)-DeckA)\n"
        "Turn: Turn 2 (Ai(2)-DeckB)\n"
        f"Turn: Turn 80 (Ai({active_seat})-{name})\n"
        f"Turn: Turn 81 (Ai({active_seat})-{name})\n"
    )


def _make_seq_runner(sims: list[SimResult]):
    """A runner that returns queued SimResults in order, one per run() call."""
    class _SeqRunner:
        def __init__(self):
            self.idx = 0
            self.calls: list[list[str]] = []

        def run(self, deck_filenames, num_games, **kwargs):
            self.calls.append(list(deck_filenames))
            s = sims[self.idx]
            self.idx += 1
            return s
    return _SeqRunner()


def test_timeout_salvages_batch_and_credits_active_seat_to_deck_a(tmp_path):
    """Game 1 completes (deck_a seat 1 wins). Game 2 times out while it is
    deck_a's turn (deck_a sits in seat 2 on the odd game) -> that game is
    credited to wins_a and counted; status is done, not failed."""
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    sims = [
        # i=0 order [DeckA, DeckB, f1, f2] -> deck_a seat 1, A wins decisively.
        SimResult(cmd=["fake"], returncode=0, duration_sec=1.0,
                  stdout=_ab_canned_stdout(
                      10, 1, "DeckA",
                      seats=["DeckA", "DeckB", "filler1", "filler2"]),
                  stderr="", timed_out=False, error=None),
        # i=1 order [DeckB, DeckA, f1, f2] -> deck_a seat 2. Loop on seat 2.
        _timeout_sim(_in_progress_turns(active_seat=2)),
    ]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=2,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )

    assert result.status == _AB_STATUS_DONE          # salvaged, NOT failed
    assert result.games == 2                         # completed + salvaged
    assert result.wins_a == 2                        # seat1 win + seat2 loop
    assert result.wins_b == 0
    assert result.error is not None
    assert "active seat 2" in result.error
    # ASCII-only note (no unicode dashes etc.)
    assert result.error.isascii()


def test_timeout_credits_deck_b_when_it_is_active(tmp_path):
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    # Single game (i=0): order [DeckA, DeckB, f1, f2] -> deck_b seat 2 loops.
    sims = [_timeout_sim(_in_progress_turns(active_seat=2))]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=1,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )
    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_b == 1
    assert result.wins_a == 0


def test_timeout_on_filler_seat_credits_neither(tmp_path):
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    # i=0: order [DeckA, DeckB, f1, f2]; filler in seat 3 loops.
    sims = [_timeout_sim(_in_progress_turns(active_seat=3, name="filler1"))]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=1,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )
    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_a == 0 and result.wins_b == 0
    assert result.error is not None and result.error.isascii()
    assert "filler" in result.error.lower() or "none" in result.error.lower()


def test_timeout_with_no_turn_line_credits_none(tmp_path):
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    sims = [_timeout_sim("Boot noise, no Turn lines at all\n")]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=1,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )
    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_a == 0 and result.wins_b == 0


def test_nonzero_exit_without_timeout_still_fails(tmp_path):
    """A genuine Forge crash (non-zero exit, timed_out=False) must still fail
    the batch - the salvage path only applies to sim.timed_out."""
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_FAILED

    deck_a = tmp_path / "a.dck"
    deck_b = tmp_path / "b.dck"
    deck_a.write_text("", encoding="utf-8")
    deck_b.write_text("", encoding="utf-8")

    sims = [SimResult(cmd=["fake"], returncode=1, duration_sec=0.5,
                      stdout="boom\n", stderr="trace", timed_out=False,
                      error="Forge exited with code 1")]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=2,
        runner=runner, fillers=["f1.dck", "f2.dck"],
    )
    assert result.status == _AB_STATUS_FAILED


# --- Draw -> life-leader credited as a seat win (policy point 1 applied) -----


def test_per_game_draw_with_unique_life_leader_credits_that_seat(tmp_path):
    """A per-game turn-cap draw with a unique ending_life leader (seat 1 =
    deck_a) credits deck_a a win even though Forge printed no 'has won!'."""
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    # i=0: order [DeckA, DeckB, f1, f2]; seat 1 ends with the most life.
    draw_stdout = (
        "Turn: Turn 1 (Ai(1)-DeckA)\n"
        "Turn: Turn 1 (Ai(2)-DeckB)\n"
        "Turn: Turn 1 (Ai(3)-filler1)\n"
        "Turn: Turn 1 (Ai(4)-filler2)\n"
        "Life: Life: Ai(1)-DeckA 40 > 31\n"   # unique top
        "Life: Life: Ai(2)-DeckB 40 > 14\n"
        "Life: Life: Ai(3)-filler1 40 > 6\n"
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"
        "Match Result: Ai(1)-DeckA: 0 Ai(2)-DeckB: 0 "
        "Ai(3)-filler1: 0 Ai(4)-filler2: 0\n"
    )
    sims = [SimResult(cmd=["fake"], returncode=0, duration_sec=1.0,
                      stdout=draw_stdout, stderr="", timed_out=False, error=None)]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=1,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )
    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_a == 1   # draw resolved to seat-1 life leader = deck_a
    assert result.wins_b == 0


def test_per_game_draw_with_tied_top_life_credits_neither(tmp_path):
    """A draw with no unique life leader stays neutral - neither deck credited."""
    from commander_builder.forge_runner import run_ab_simulation, _AB_STATUS_DONE

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    draw_stdout = (
        "Turn: Turn 1 (Ai(1)-DeckA)\n"
        "Turn: Turn 1 (Ai(2)-DeckB)\n"
        "Life: Life: Ai(1)-DeckA 40 > 20\n"
        "Life: Life: Ai(2)-DeckB 40 > 20\n"   # tie at the top
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"
        "Match Result: Ai(1)-DeckA: 0 Ai(2)-DeckB: 0 "
        "Ai(3)-filler1: 0 Ai(4)-filler2: 0\n"
    )
    sims = [SimResult(cmd=["fake"], returncode=0, duration_sec=1.0,
                      stdout=draw_stdout, stderr="", timed_out=False, error=None)]
    runner = _make_seq_runner(sims)

    result = run_ab_simulation(
        deck_a, deck_b, games=1,
        runner=runner, fillers=["filler1.dck", "filler2.dck"],
    )
    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_a == 0 and result.wins_b == 0


# --- run_gauntlet_simulation — one test deck vs a fixed 3-deck gauntlet ------


def _gauntlet_draw_stdout(seats: list[str], ending_lives: list[int],
                          end_turn: int = 50) -> str:
    """Synthesize a turn-cap draw: per-seat Turn seeds + Life lines (so the
    analyzer can resolve the draw to the strictly-highest ending-life seat),
    the draw marker, and a no-winner Game Result."""
    turn_seeds = "\n".join(
        f"Turn: Turn 1 (Ai({i + 1})-{n})" for i, n in enumerate(seats)
    )
    life_lines = "\n".join(
        f"Life: Life: Ai({i + 1})-{n} 40 > {ending_lives[i]}"
        for i, n in enumerate(seats)
    )
    return (
        f"{turn_seeds}\n{life_lines}\n"
        f"Game Outcome: Turn {end_turn}\n"
        f"Stopping slow match as draw\n"
        f"Game Result: Game 1 ended in 240000 ms\n"
    )


def test_run_gauntlet_simulation_tallies_by_test_seat(tmp_path):
    """wins/losses/draws are attributed to the TEST seat (which the harness
    controls via rotation), across a decisive win, a decisive loss won by a
    gauntlet seat, a draw resolved to the test seat, and a true draw."""
    from commander_builder.forge_runner import (
        run_gauntlet_simulation, _AB_STATUS_DONE)

    test_deck = tmp_path / "[USER] TestDeck [B4].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")
    gauntlet = ["G1.dck", "G2.dck", "G3.dck"]

    sims = [
        # g0: test seat 1, TestDeck wins -> WIN
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_ab_canned_stdout(
                      10, 1, "TestDeck",
                      seats=["TestDeck", "G1", "G2", "G3"])),
        # g1: test seat 2, gauntlet G1 (seat 1) wins -> LOSS
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_ab_canned_stdout(
                      9, 1, "G1",
                      seats=["G1", "TestDeck", "G2", "G3"])),
        # g2: test seat 3, draw resolved to seat 3 (highest life) -> WIN
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_gauntlet_draw_stdout(
                      seats=["G1", "G2", "TestDeck", "G3"],
                      ending_lives=[10, 5, 30, 0])),
        # g3: test seat 4, true draw (tie at top, no unique leader) -> DRAW
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_gauntlet_draw_stdout(
                      seats=["G1", "G2", "G3", "TestDeck"],
                      ending_lives=[20, 20, 5, 5])),
    ]
    result = run_gauntlet_simulation(
        test_deck, gauntlet, games=4, runner=_make_seq_runner(sims))

    assert result.status == _AB_STATUS_DONE
    assert result.games == 4
    assert (result.wins, result.losses, result.draws) == (2, 1, 1)
    assert result.wins + result.losses + result.draws == result.games


def test_run_gauntlet_simulation_rotates_test_deck_through_all_seats(tmp_path):
    """The test deck must occupy seats 1,2,3,4 over four games; gauntlet decks
    fill the remaining seats in fixed order."""
    from commander_builder.forge_runner import run_gauntlet_simulation

    test_deck = tmp_path / "[USER] T [B3].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")
    gauntlet = ["G1.dck", "G2.dck", "G3.dck"]

    sims = [
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_ab_canned_stdout(5, 1, "x", seats=["a", "b", "c", "d"]))
        for _ in range(4)
    ]
    result = run_gauntlet_simulation(
        test_deck, gauntlet, games=4, runner=_make_seq_runner(sims))

    positions = [order.index(test_deck.name) for order in result.seat_orders]
    assert positions == [0, 1, 2, 3]
    # Gauntlet order is preserved among the non-test seats.
    for order in result.seat_orders:
        non_test = [d for d in order if d != test_deck.name]
        assert non_test == gauntlet


def test_run_gauntlet_simulation_timeout_salvage_credits_active_seat(tmp_path):
    """A looping game is credited to the active seat: a WIN when that's the
    test seat, then the batch stops with what it has."""
    from commander_builder.forge_runner import (
        run_gauntlet_simulation, _AB_STATUS_DONE)

    test_deck = tmp_path / "[USER] T [B4].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")
    gauntlet = ["G1.dck", "G2.dck", "G3.dck"]

    # game 0: test seat 1; last Turn line names seat 1 (the test deck).
    looping = SimResult(
        cmd=["x"], returncode=None, duration_sec=1.0, stderr="",
        timed_out=True, error="Timed out",
        stdout="Turn: Turn 30 (Ai(1)-T)\n")
    result = run_gauntlet_simulation(
        test_deck, gauntlet, games=4, runner=_make_seq_runner([looping]))

    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert (result.wins, result.losses, result.draws) == (1, 0, 0)


def test_run_gauntlet_simulation_loop_unattributed_keeps_completed_games(tmp_path):
    """A looping game whose partial stdout carries NO Turn line (the real
    shape: Forge's SimulateMatch prints the game log only AFTER a game
    completes, so a hung game's capture is just the pre-game header) must NOT
    be counted as a phantom draw. The row ends as an honest short
    'loop_unattributed' row: the hung game is excluded, the completed games
    are kept, and the error says why instead of 'credited to active seat
    None'."""
    from commander_builder.forge_runner import (
        run_gauntlet_simulation, _AB_STATUS_LOOP_UNATTRIBUTED)

    test_deck = tmp_path / "[USER] T [B4].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")
    gauntlet = ["G1.dck", "G2.dck", "G3.dck"]

    # Verbatim pre-game header shapes from a live Forge 2.0.12 capture —
    # everything a hung game's partial stdout actually contains.
    hung_partial_stdout = (
        "Simulation mode\n"
        "Ai(1)-Salty IronMan vs Ai(2)-Eldrazi Incursion [M3C] [2024] vs "
        "Ai(3)-Graveyard Overdrive [M3C] [2024] vs "
        "Ai(4)-Creative Energy [M3C] [2024] - one game of Commander\n"
        "SVar 'Double' not found in ability, fallback to Card "
        "(Overclocked Electromancer). Ability is ()\n"
    )

    sims = [
        # g0: test seat 1 wins decisively -> WIN
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_ab_canned_stdout(
                      10, 1, "T", seats=["T", "G1", "G2", "G3"])),
        # g1: test seat 2, gauntlet G1 (seat 1) wins -> LOSS
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0, stderr="",
                  timed_out=False, error=None,
                  stdout=_ab_canned_stdout(
                      9, 1, "G1", seats=["G1", "T", "G2", "G3"])),
        # g2: hangs -> per-game timeout kill; partial stdout has no Turn line.
        _timeout_sim(hung_partial_stdout),
    ]
    result = run_gauntlet_simulation(
        test_deck, gauntlet, games=40, runner=_make_seq_runner(sims))

    assert result.status == _AB_STATUS_LOOP_UNATTRIBUTED
    assert result.games == 2                       # hung game NOT counted
    assert (result.wins, result.losses, result.draws) == (1, 1, 0)
    assert result.wins + result.losses + result.draws == result.games
    assert result.error is not None and result.error.isascii()
    assert "loop at game 3" in result.error
    assert "kept 2 completed games" in result.error
    assert "seat None" not in result.error         # the old misleading string


def test_turn_line_regex_matches_real_forge_2012_shapes():
    """_AB_TURN_LINE vs VERBATIM Turn lines from vendor Forge 2.0.12 logs
    (plain and bracketed deck names) — documents that the regex is NOT why
    loop-abort seat attribution fails; a hung game simply has no Turn line
    because Forge dumps the game log only after a game completes."""
    from commander_builder.forge_runner import _last_active_seat

    real_lines = (
        "Turn: Turn 17 (Ai(2)-Black Mage Blitz)\n"
        "Turn: Turn 18 (Ai(3)-Celestial Tribunal)\n"
        "Turn: Turn 19 (Ai(4)-Eldrazi Incursion [M3C] [2024])\n"
    )
    assert _last_active_seat(real_lines) == 4

    # And the real hung-game capture (pre-game header only) yields None.
    header_only = (
        "Simulation mode\n"
        "Ai(1)-Salty IronMan vs Ai(2)-Eldrazi Incursion [M3C] [2024] vs "
        "Ai(3)-Graveyard Overdrive [M3C] [2024] vs "
        "Ai(4)-Creative Energy [M3C] [2024] - one game of Commander\n"
    )
    assert _last_active_seat(header_only) is None


def test_run_gauntlet_simulation_skips_on_wrong_gauntlet_size(tmp_path):
    from commander_builder.forge_runner import (
        run_gauntlet_simulation, _AB_STATUS_SKIPPED)

    test_deck = tmp_path / "[USER] T [B3].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")
    result = run_gauntlet_simulation(
        test_deck, ["G1.dck", "G2.dck"], games=4,
        runner=_make_seq_runner([]))
    assert result.status == _AB_STATUS_SKIPPED
    assert "3 gauntlet decks" in (result.error or "")


# --- run_ab_parallel — single matchup chunked across profiles ---------------


def test_even_chunks_splits_into_balanced_even_sizes():
    from commander_builder.forge_runner import _even_chunks

    sizes = _even_chunks(100, 12)
    assert sum(sizes) == 100
    assert all(s % 2 == 0 for s in sizes)  # every chunk seat-balanced
    assert max(sizes) - min(sizes) <= 2    # balanced
    # Fewer games than parts -> at most `games` chunks, none empty.
    assert _even_chunks(3, 8) == [2, 1] or sum(_even_chunks(3, 8)) == 3
    assert _even_chunks(1, 8) == [1]
    assert _even_chunks(0, 8) == []


def test_even_chunks_odd_total_puts_leftover_on_first_chunk():
    from commander_builder.forge_runner import _even_chunks

    sizes = _even_chunks(101, 4)
    assert sum(sizes) == 101
    # Exactly one odd chunk (the leftover game); the rest stay even.
    assert sum(1 for s in sizes if s % 2 == 1) == 1


def _stub_runners(monkeypatch):
    """_runner_for builds real ForgeRunners (needs the vendor jar); stub it so
    the parallel tests stay environment-independent. _sim_fn ignores the runner."""
    monkeypatch.setattr(
        "commander_builder.forge_runner._runner_for",
        lambda profile: object(),
    )


def test_run_ab_parallel_aggregates_chunk_results(tmp_path, monkeypatch):
    """100 games fanned across 4 fake profiles must sum per-seat wins, games,
    and seat_orders back into one ABResult identical in shape to a serial run.
    """
    from commander_builder.forge_runner import run_ab_parallel, ABResult
    _stub_runners(monkeypatch)

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    profiles = [tmp_path / "forge", tmp_path / "forge2",
                tmp_path / "forge3", tmp_path / "forge4"]

    seen_sizes: list[int] = []

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        seen_sizes.append(games)
        # Each chunk: A wins 60%, B wins 40%, avg turns A=10, B=8.
        wa, wb = (games * 3) // 5, games - (games * 3) // 5
        return ABResult(
            deck_a=da.name, deck_b=db.name,
            wins_a=wa, wins_b=wb, games=games,
            avg_turns_a=10.0, avg_turns_b=8.0,
            # Every win carried an end_turn in this fake, so the turn-sample
            # counts (what recombination weights by) equal the win counts.
            turn_samples_a=wa, turn_samples_b=wb,
            status="done",
            seat_orders=[[da.name, db.name, "f1", "f2"]] * games,
        )

    result = run_ab_parallel(
        deck_a, deck_b, games=100,
        fillers=["f1.dck", "f2.dck"],
        profiles=profiles, max_workers=4,
        _sim_fn=fake_sim,
    )

    assert result.status == "done"
    assert result.games == 100
    assert sum(seen_sizes) == 100
    assert len(seen_sizes) == 4              # one chunk per profile
    assert result.wins_a + result.wins_b == 100
    assert result.wins_a > result.wins_b     # A favored in every chunk
    assert len(result.seat_orders) == 100
    # Weighted avg turns collapses to the per-chunk constants.
    assert result.avg_turns_a == 10.0
    assert result.avg_turns_b == 8.0


def test_run_ab_parallel_weights_avg_turns_by_turn_samples(tmp_path, monkeypatch):
    """Chunk avg_turns are means over the chunk's turn-SAMPLE count (wins
    with a known end_turn) — a timeout-salvaged win contributes to wins_a
    but NOT to the mean. Recombination must therefore weight by the sample
    counts the chunks carry, not by wins (the old bug)."""
    from commander_builder.forge_runner import run_ab_parallel, ABResult
    _stub_runners(monkeypatch)

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")
    profiles = [tmp_path / "forge", tmp_path / "forge2"]

    # list.pop() is atomic under the GIL, so the two worker threads can't
    # both receive the same canned chunk.
    chunks = [
        # Chunk 1: A won twice but ONE win was a timeout salvage with no
        # end_turn — its 12.0 mean covers only 1 sample.
        ABResult(wins_a=2, wins_b=1, games=4,
                 avg_turns_a=12.0, avg_turns_b=9.0,
                 turn_samples_a=1, turn_samples_b=1, status="done"),
        # Chunk 2: both of A's wins sampled.
        ABResult(wins_a=2, wins_b=2, games=4,
                 avg_turns_a=8.0, avg_turns_b=9.0,
                 turn_samples_a=2, turn_samples_b=2, status="done"),
    ]

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        return chunks.pop()

    result = run_ab_parallel(
        deck_a, deck_b, games=8,
        fillers=["f1.dck", "f2.dck"],
        profiles=profiles, max_workers=2,
        _sim_fn=fake_sim,
    )

    assert result.wins_a == 4
    assert result.turn_samples_a == 3
    # Sample-weighted: (12.0*1 + 8.0*2) / 3 = 9.33. The win-weighted bug
    # would have produced (12.0*2 + 8.0*2) / 4 = 10.0.
    assert result.avg_turns_a == pytest.approx(9.33, abs=0.01)
    assert result.avg_turns_b == pytest.approx(9.0, abs=0.01)
    assert result.turn_samples_b == 3


def test_run_ab_parallel_keeps_wins_when_one_chunk_fails(tmp_path, monkeypatch):
    """A crash in one chunk marks the aggregate failed but still reports the
    wins from the chunks that completed (no silent discard of good games)."""
    from commander_builder.forge_runner import run_ab_parallel, ABResult
    _stub_runners(monkeypatch)

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")
    profiles = [tmp_path / "forge", tmp_path / "forge2"]

    calls = {"n": 0}

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        calls["n"] += 1
        if calls["n"] == 1:
            return ABResult(deck_a=da.name, deck_b=db.name, wins_a=games,
                            games=games, status="done")
        return ABResult(deck_a=da.name, deck_b=db.name, status="failed",
                        error="Forge exited with code 1")

    result = run_ab_parallel(
        deck_a, deck_b, games=50,
        fillers=["f1.dck", "f2.dck"],
        profiles=profiles, max_workers=2,
        _sim_fn=fake_sim,
    )

    assert result.status == "failed"
    assert result.wins_a > 0                 # completed chunk's wins survive
    assert "code 1" in (result.error or "")


def test_run_ab_parallel_single_profile_is_serial(tmp_path, monkeypatch):
    """One profile -> one chunk holding all games (graceful degenerate case)."""
    from commander_builder.forge_runner import run_ab_parallel, ABResult
    _stub_runners(monkeypatch)

    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    sizes: list[int] = []

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        sizes.append(games)
        return ABResult(deck_a=da.name, deck_b=db.name, wins_a=games,
                        games=games, status="done")

    result = run_ab_parallel(
        deck_a, deck_b, games=40,
        fillers=["f1.dck", "f2.dck"],
        profiles=[tmp_path / "forge"],
        _sim_fn=fake_sim,
    )
    assert sizes == [40]
    assert result.games == 40 and result.status == "done"


# ---------------------------------------------------------------------------
# ForgeRunner.locate() — multi-jar selection by parsed semver
# ---------------------------------------------------------------------------

def test_locate_picks_highest_version_jar_not_lex(tmp_path, monkeypatch):
    """When several forge-gui-desktop-*.jar files coexist, locate() must
    pick by parsed version (not lex), and prefer the fat jar within a
    version. Lex sort puts "2.0.10" before "2.0.12" because "0" < "2",
    so the prior `sorted(...)[0]` chose the OLDER fat jar."""
    from commander_builder import forge_runner

    # Stage a vendor/forge dir with several jars, including the off-by-lex pair.
    fake_forge = tmp_path / "forge"
    fake_forge.mkdir()
    (fake_forge / "forge-gui-desktop-2.0.9-jar-with-dependencies.jar").write_bytes(b"x")
    (fake_forge / "forge-gui-desktop-2.0.10-jar-with-dependencies.jar").write_bytes(b"x")
    (fake_forge / "forge-gui-desktop-2.0.12-jar-with-dependencies.jar").write_bytes(b"x")
    (fake_forge / "forge-gui-desktop-2.0.12.jar").write_bytes(b"x")  # thin jar

    fake_jre = tmp_path / "jre" / "bin"
    fake_jre.mkdir(parents=True)
    fake_java = fake_jre / "java.exe"
    fake_java.write_bytes(b"x")

    monkeypatch.setattr(forge_runner, "VENDOR_FORGE", fake_forge)
    monkeypatch.setattr(forge_runner, "VENDOR_JRE", tmp_path / "jre")

    runner = forge_runner.ForgeRunner.locate()
    assert runner.forge_jar.name == "forge-gui-desktop-2.0.12-jar-with-dependencies.jar", (
        f"expected the highest-version FAT jar; got {runner.forge_jar.name!r}"
    )


def test_locate_raises_when_no_jar_present(tmp_path, monkeypatch):
    from commander_builder import forge_runner
    empty_forge = tmp_path / "forge"
    empty_forge.mkdir()
    fake_jre = tmp_path / "jre" / "bin"
    fake_jre.mkdir(parents=True)
    (fake_jre / "java.exe").write_bytes(b"x")
    monkeypatch.setattr(forge_runner, "VENDOR_FORGE", empty_forge)
    monkeypatch.setattr(forge_runner, "VENDOR_JRE", tmp_path / "jre")
    with pytest.raises(FileNotFoundError, match="Forge jar not found"):
        forge_runner.ForgeRunner.locate()


# ---------------------------------------------------------------------------
# keep_partial_output — streaming capture for the timeout-salvage path
# ---------------------------------------------------------------------------
#
# The looper-credit half of the timeout salvage (4f9252b) was a no-op in
# production: run_ab_simulation / run_gauntlet_simulation called runner.run()
# without stream/on_line, which routed through _run_blocking
# (subprocess.run). On a timeout kill the buffered Forge stdout is lost, so
# _last_active_seat found no Turn line and salvaged rows read "credited to
# none (no Turn line found)". keep_partial_output=True routes through
# _run_streaming, which accumulates each line as it arrives, so everything
# Forge emitted before the kill survives for seat attribution.


def _fake_streaming_factory(stdout: str, *, timed_out: bool, calls: dict):
    def fake_streaming(cmd, timeout, cwd, *, stream=True, on_line=None,
                       abort_check=None):
        calls["streaming"] = {"stream": stream}
        error = f"Timed out after {timeout}s" if timed_out else None
        rc = None if timed_out else 0
        return stdout, "", rc, timed_out, error
    return fake_streaming


def _fake_blocking_factory(stdout: str, *, timed_out: bool, calls: dict):
    def fake_blocking(cmd, timeout, cwd):
        calls["blocking"] = True
        error = f"Timed out after {timeout}s" if timed_out else None
        rc = None if timed_out else 0
        return stdout, "", rc, timed_out, error
    return fake_blocking


def test_run_keep_partial_output_routes_through_streaming(tmp_path):
    """keep_partial_output=True must use the streaming reader (without
    echoing to the terminal) so stdout emitted before a timeout kill
    survives for downstream parsing."""
    from commander_builder.forge_runner import ForgeRunner

    calls: dict = {}
    runner = ForgeRunner(java_path=tmp_path / "java",
                         forge_jar=tmp_path / "forge.jar",
                         forge_dir=tmp_path)
    with patch("commander_builder.forge_runner._run_streaming",
               side_effect=_fake_streaming_factory(
                   "streamed output\n", timed_out=False, calls=calls)), \
         patch("commander_builder.forge_runner._run_blocking",
               side_effect=_fake_blocking_factory(
                   "blocked output\n", timed_out=False, calls=calls)):
        sim = runner.run(
            ["a.dck", "b.dck", "c.dck", "d.dck"], num_games=1,
            keep_partial_output=True,
        )

    assert "blocking" not in calls
    assert calls["streaming"]["stream"] is False  # no terminal echo
    assert sim.stdout == "streamed output\n"


def test_ab_timeout_salvage_survives_blocking_stdout_loss(tmp_path):
    """Regression: run_ab_simulation must route runner.run() through the
    streaming reader. We model the real pathology — the blocking path
    returns EMPTY stdout on a timeout kill, the streaming path returns the
    Turn lines — so only the streaming route lets the salvage credit the
    active seat instead of 'credited to none'."""
    from commander_builder.forge_runner import (
        ForgeRunner, run_ab_simulation, _AB_STATUS_DONE,
    )

    calls: dict = {}
    turn_stdout = _in_progress_turns(active_seat=1, name="DeckA")
    deck_a = tmp_path / "[USER] DeckA [B3].dck"
    deck_b = tmp_path / "[USER] DeckB [B3].dck"
    deck_a.write_text("[Main]\n", encoding="utf-8")
    deck_b.write_text("[Main]\n", encoding="utf-8")

    runner = ForgeRunner(java_path=tmp_path / "java",
                         forge_jar=tmp_path / "forge.jar",
                         forge_dir=tmp_path)
    with patch("commander_builder.forge_runner._run_streaming",
               side_effect=_fake_streaming_factory(
                   turn_stdout, timed_out=True, calls=calls)), \
         patch("commander_builder.forge_runner._run_blocking",
               side_effect=_fake_blocking_factory(
                   "", timed_out=True, calls=calls)):
        result = run_ab_simulation(
            deck_a, deck_b, games=2,
            runner=runner, fillers=["filler1.dck", "filler2.dck"],
        )

    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins_a == 1  # loop credited to active seat 1 == deck_a
    assert result.wins_b == 0
    assert "credited to active seat 1" in result.error


def test_gauntlet_timeout_salvage_survives_blocking_stdout_loss(tmp_path):
    """Same regression as above for the gauntlet salvage site: the test
    deck sits in seat 1 on game 1; the loop must be credited to it as a
    win rather than falling through to 'credited to none' -> draw."""
    from commander_builder.forge_runner import (
        ForgeRunner, run_gauntlet_simulation, _AB_STATUS_DONE,
    )

    calls: dict = {}
    turn_stdout = _in_progress_turns(active_seat=1, name="TestDeck")
    test_deck = tmp_path / "[USER] TestDeck [B3].dck"
    test_deck.write_text("[Main]\n", encoding="utf-8")

    runner = ForgeRunner(java_path=tmp_path / "java",
                         forge_jar=tmp_path / "forge.jar",
                         forge_dir=tmp_path)
    with patch("commander_builder.forge_runner._run_streaming",
               side_effect=_fake_streaming_factory(
                   turn_stdout, timed_out=True, calls=calls)), \
         patch("commander_builder.forge_runner._run_blocking",
               side_effect=_fake_blocking_factory(
                   "", timed_out=True, calls=calls)):
        result = run_gauntlet_simulation(
            test_deck, ["g1.dck", "g2.dck", "g3.dck"], games=1,
            runner=runner,
        )

    assert result.status == _AB_STATUS_DONE
    assert result.games == 1
    assert result.wins == 1  # loop credited to the test deck's seat
    assert result.draws == 0
    assert "credited to active seat 1" in result.error
