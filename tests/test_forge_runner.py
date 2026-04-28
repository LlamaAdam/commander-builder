"""forge_runner tests — focused on the streaming/non-streaming dispatch.

The actual Forge subprocess can't be unit-tested without a Forge install, so
we mock at the subprocess boundary. The blocking path was already exercised
by every live integration script in `scripts/`; this file pins the new
streaming code (GAP-008) so it doesn't drift.
"""
from unittest.mock import MagicMock, patch

import pytest

from commander_builder.forge_runner import (
    SimResult,
    _run_blocking,
    _run_streaming,
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
