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
