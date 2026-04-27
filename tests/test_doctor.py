"""commander-doctor tests — each check exercised in isolation."""
import json
import socket
import urllib.error

import pytest

from commander_builder.doctor import (
    DoctorReport,
    GREEN,
    RED,
    YELLOW,
    _check_anthropic_key,
    _check_anthropic_sdk,
    _check_cache_dir,
    _check_ollama,
    _check_python,
    format_text,
    run_doctor,
)


def test_python_check_passes_on_supported_version():
    result = _check_python()
    assert result.status == GREEN
    assert "executable" in result.detail


def test_check_anthropic_key_yellow_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = _check_anthropic_key()
    assert result.status == YELLOW
    assert "not set" in result.message


def test_check_anthropic_key_green_when_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-with-some-length")
    result = _check_anthropic_key()
    assert result.status == GREEN
    assert "ANTHROPIC_API_KEY set" in result.message


def test_check_anthropic_sdk_yellow_when_uninstalled(monkeypatch):
    """Force the import to fail so the check reports YELLOW."""
    import sys, builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "anthropic":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    sys.modules.pop("anthropic", None)
    result = _check_anthropic_sdk()
    assert result.status == YELLOW
    assert "not installed" in result.message


def test_check_cache_dir_creates_and_writes(tmp_path):
    cache = tmp_path / "cache"
    result = _check_cache_dir("test_cache", cache)
    assert result.status == GREEN
    assert cache.exists()


def test_check_cache_dir_red_when_unwritable(tmp_path, monkeypatch):
    """Mock mkdir to fail. Path itself doesn't matter."""
    target = tmp_path / "nope"
    def fail_mkdir(*a, **kw):
        raise OSError("read-only filesystem")
    monkeypatch.setattr("pathlib.Path.mkdir", fail_mkdir)
    result = _check_cache_dir("test_cache", target)
    assert result.status == RED


def test_check_ollama_yellow_when_unreachable(monkeypatch):
    def network_down(url, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", network_down)
    result = _check_ollama()
    assert result.status == YELLOW
    assert "not reachable" in result.message


def test_check_ollama_green_when_reachable(monkeypatch):
    payload = json.dumps({"models": [{"name": "llama3.2:3b"}]}).encode("utf-8")

    class FakeResp:
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(url, timeout=None):
        return FakeResp(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = _check_ollama()
    assert result.status == GREEN
    assert "1 model" in result.message


def test_check_ollama_yellow_on_timeout(monkeypatch):
    def slow(url, timeout=None):
        raise socket.timeout("too slow")
    monkeypatch.setattr("urllib.request.urlopen", slow)
    result = _check_ollama()
    assert result.status == YELLOW


# --- DoctorReport aggregation ---------------------------------------------

def test_worst_status_picks_red_over_yellow_over_green():
    from commander_builder.doctor import CheckResult
    r = DoctorReport(checks=[
        CheckResult("a", GREEN, ""),
        CheckResult("b", YELLOW, ""),
        CheckResult("c", RED, ""),
    ])
    assert r.worst_status == RED
    assert r.exit_code == 1


def test_worst_status_yellow_when_no_red():
    from commander_builder.doctor import CheckResult
    r = DoctorReport(checks=[
        CheckResult("a", GREEN, ""),
        CheckResult("b", YELLOW, ""),
    ])
    assert r.worst_status == YELLOW
    assert r.exit_code == 2


def test_worst_status_green_when_all_green():
    from commander_builder.doctor import CheckResult
    r = DoctorReport(checks=[CheckResult("a", GREEN, "")])
    assert r.worst_status == GREEN
    assert r.exit_code == 0


# --- Full run + text rendering --------------------------------------------

def test_run_doctor_returns_report_with_all_checks():
    report = run_doctor(skip_ollama=True)
    names = [c.name for c in report.checks]
    assert "python" in names
    assert "package" in names
    assert "forge" in names
    assert "knowledge_log" in names


def test_format_text_includes_all_check_lines():
    report = run_doctor(skip_ollama=True)
    text = format_text(report)
    assert "Commander Builder" in text
    assert "Worst status" in text
    for c in report.checks:
        assert c.name in text


def test_doctor_to_dict_round_trips():
    report = run_doctor(skip_ollama=True)
    d = report.to_dict()
    assert "worst_status" in d
    assert "exit_code" in d
    assert len(d["checks"]) == len(report.checks)
