"""verify_forge subprocess-encoding tests.

Pin the cp1252-vs-UTF-8 Windows trap: `subprocess.run(..., text=True)` with
no `encoding=` decodes the child's output with the LOCALE codec (cp1252 on
Windows) in STRICT mode. Forge emits UTF-8, and deck names with emoji /
non-Latin characters occur in practice — so a single non-cp1252-decodable
byte used to raise UnicodeDecodeError *inside* subprocess.run and turn into
a failed verification with no stdout captured. These tests pin the explicit
`encoding="utf-8", errors="replace"` kwargs on every verify_forge launch
site, plus the defensive handling of TimeoutExpired.stdout (which can be
bytes on POSIX, or None, even when encoding= was passed).

Same convention as test_forge_runner.py: mock at the subprocess boundary,
no real Java/Forge needed.
"""
from pathlib import Path
from unittest.mock import MagicMock

import subprocess

import pytest

from commander_builder.verify_forge import find_java, run_sim


# Characters that are valid UTF-8 but NOT representable/decodable the same
# way in cp1252 — a locale-codec strict decode of their UTF-8 bytes fails.
NON_CP1252_STDOUT = "Match 1: \U0001f409 Dragon deck vs Æther Vial combo\nMatch Result: ok\n"


def _capturing_run(calls, *, stdout=NON_CP1252_STDOUT, stderr="", returncode=0):
    """A subprocess.run stand-in that records its kwargs and returns a
    CompletedProcess-shaped mock. Recording the kwargs is the point: the
    test pins that encoding= / errors= actually reach subprocess.run —
    that is the boundary where the locale-codec strict decode would
    otherwise happen, invisible to any output-only assertion."""
    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)
    return fake_run


def _sim_paths(tmp_path: Path) -> dict:
    """Minimal on-disk layout for run_sim: a jar path (parent = cwd) and a
    deck file. Neither is opened — subprocess.run is mocked."""
    jar = tmp_path / "install" / "forge-gui-desktop-2.0.12.jar"
    jar.parent.mkdir(parents=True)
    deck = tmp_path / "decks" / "Test Deck.dck"
    deck.parent.mkdir(parents=True)
    deck.write_text("[metadata]\nName=Test Deck\n", encoding="utf-8")
    return {"jar": jar, "deck": deck, "out": tmp_path / "verify_output"}


def test_run_sim_pins_utf8_encoding_kwargs_and_survives_non_cp1252_output(
    tmp_path, monkeypatch,
):
    """A completed sim whose stdout contains emoji / Æ must parse fine — and
    the encoding kwargs must be pinned at the subprocess.run boundary."""
    paths = _sim_paths(tmp_path)
    calls: list = []
    monkeypatch.setattr(
        "commander_builder.verify_forge.subprocess.run", _capturing_run(calls),
    )

    info = run_sim(
        java="fake-java",
        jar=paths["jar"],
        deck_paths=[paths["deck"]],
        game_format="commander",
        num_games=1,
        output_dir=paths["out"],
        label="enc",
    )

    # No UnicodeDecodeError swallowed into the catch-all: the run completed.
    assert info["error"] is None
    assert info["returncode"] == 0

    # THE pin: text-mode decode must be UTF-8 with replacement, never the
    # implicit locale codec (cp1252 strict on Windows).
    assert len(calls) == 1
    kwargs = calls[0][1]
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"

    # The non-cp1252 characters round-trip into the captured stdout file.
    captured = Path(info["stdout_path"]).read_text(encoding="utf-8")
    assert "\U0001f409" in captured
    assert "Æther" in captured


def test_find_java_version_probe_pins_utf8_encoding_kwargs(monkeypatch):
    """The `java -version` probe is ASCII in practice, but it must use the
    same explicit UTF-8+replace decode as every other launch site so a
    localized/odd JVM banner can never crash discovery via the implicit
    locale-codec strict decode."""
    calls: list = []
    monkeypatch.setattr(
        "commander_builder.verify_forge.subprocess.run",
        _capturing_run(calls, stdout="", stderr='openjdk version "21.0.2"\n'),
    )
    # Force the PATH fallback so the test doesn't depend on vendor/jre
    # existing in this checkout.
    monkeypatch.setattr(
        "commander_builder.verify_forge.shutil.which", lambda _: "fake-java",
    )
    monkeypatch.setattr(
        "commander_builder.verify_forge.VENDOR_JRE", Path("does-not-exist"),
    )

    java, version = find_java()

    assert java == "fake-java"
    assert version == 'openjdk version "21.0.2"'
    assert len(calls) == 1
    kwargs = calls[0][1]
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"


def test_run_sim_timeout_with_bytes_stdout_is_decoded_not_crashed(
    tmp_path, monkeypatch,
):
    """TimeoutExpired.stdout is BYTES on POSIX (CPython re-raises the raw
    pipe contents; the text decode only happens on the CompletedProcess
    path). write_text() rejects bytes — so the handler must decode
    defensively instead of crashing inside the error path."""
    paths = _sim_paths(tmp_path)

    def raise_timeout(*a, **kw):
        # \xf0 alone is an invalid UTF-8 sequence — exercises errors="replace".
        raise subprocess.TimeoutExpired(
            cmd="fake", timeout=600, output=b"partial \xf0 bytes", stderr=b"err",
        )

    monkeypatch.setattr(
        "commander_builder.verify_forge.subprocess.run", raise_timeout,
    )

    info = run_sim(
        java="fake-java",
        jar=paths["jar"],
        deck_paths=[paths["deck"]],
        game_format="commander",
        num_games=1,
        output_dir=paths["out"],
        label="tobytes",
    )

    assert info["timed_out"] is True
    assert "Timed out" in info["error"]
    captured = Path(info["stdout_path"]).read_text(encoding="utf-8")
    assert "partial" in captured and "bytes" in captured
    # The invalid byte degraded to U+FFFD instead of raising.
    assert "�" in captured
    assert Path(info["stderr_path"]).read_text(encoding="utf-8") == "err"


def test_run_sim_timeout_with_none_stdout_writes_empty_files(
    tmp_path, monkeypatch,
):
    """TimeoutExpired.stdout/.stderr are None when nothing was captured
    before the kill — the handler must write empty files, not TypeError."""
    paths = _sim_paths(tmp_path)

    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=600)  # stdout=None

    monkeypatch.setattr(
        "commander_builder.verify_forge.subprocess.run", raise_timeout,
    )

    info = run_sim(
        java="fake-java",
        jar=paths["jar"],
        deck_paths=[paths["deck"]],
        game_format="commander",
        num_games=1,
        output_dir=paths["out"],
        label="tonone",
    )

    assert info["timed_out"] is True
    assert Path(info["stdout_path"]).read_text(encoding="utf-8") == ""
    assert Path(info["stderr_path"]).read_text(encoding="utf-8") == ""
