"""Tests for the external credentials loader.

The loader reads KEY=VALUE pairs from a file outside the repo (default
``~/.commander-builder/credentials``) and populates ``os.environ`` —
but ONLY for keys not already set. Shell env always wins so production
deployments using container secrets stay authoritative.

These tests pin:

1. **Default path resolution** — ``~/.commander-builder/credentials``
   on all platforms (uses ``Path.home()``).
2. **Override env var** — ``COMMANDER_BUILDER_CREDENTIALS=<path>``
   replaces the default.
3. **Shell env precedence** — already-set env vars are NOT overwritten.
4. **Format tolerance** — blank lines, comments, whitespace, quoted
   values all parse correctly; malformed lines are skipped with a
   warning, not a crash.
5. **Missing file** — returns empty dict with a helpful stderr hint
   (or silent when ``quiet=True``).
6. **Idempotency** — calling load_credentials() twice in one process
   only loads once (unless ``force=True``).
7. **Template scaffolder** — write_credentials_template creates the
   directory + a commented template; refuses to overwrite.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from commander_builder import _secrets


@pytest.fixture(autouse=True)
def _reset_loader_state(monkeypatch):
    """Reset the process-wide ``_loaded`` guard between tests so each
    test starts with a fresh loader. Also pin Path.home() to tmp_path
    in the fixture where needed (per-test, not autouse, to avoid
    masking the override-env-var path)."""
    monkeypatch.setattr(_secrets, "_loaded", False)
    # Strip any inherited override so tests that don't set one see
    # the default path.
    monkeypatch.delenv(_secrets._OVERRIDE_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# credentials_path() — where the loader looks
# ---------------------------------------------------------------------------

def test_default_path_is_dot_dir_in_home(monkeypatch, tmp_path):
    """Default location: ``~/.commander-builder/credentials``. Works
    on any platform because Path.home() abstracts %USERPROFILE% /
    $HOME for us."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _secrets.credentials_path() == tmp_path / ".commander-builder" / "credentials"


def test_override_env_var_replaces_default(monkeypatch, tmp_path):
    """``COMMANDER_BUILDER_CREDENTIALS=<path>`` wins over the default.
    Useful for tests, CI, and users with non-standard setups."""
    override = tmp_path / "elsewhere" / "secrets"
    monkeypatch.setenv(_secrets._OVERRIDE_ENV_VAR, str(override))
    assert _secrets.credentials_path() == override


# ---------------------------------------------------------------------------
# load_credentials — happy path
# ---------------------------------------------------------------------------

def test_load_credentials_populates_missing_env_keys(tmp_path, monkeypatch):
    """File with two keys → both land in os.environ. Returned dict
    mirrors what was applied so callers can log it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MOXFIELD_USER", raising=False)
    f = tmp_path / "creds"
    f.write_text(
        "ANTHROPIC_API_KEY=sk-ant-test\n"
        "MOXFIELD_USER=alice\n",
        encoding="utf-8",
    )
    applied = _secrets.load_credentials(path=f, quiet=True)
    assert applied == {"ANTHROPIC_API_KEY": "sk-ant-test", "MOXFIELD_USER": "alice"}
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert os.environ["MOXFIELD_USER"] == "alice"


def test_load_credentials_respects_existing_env_vars(tmp_path, monkeypatch):
    """Shell env always wins. A pre-existing ANTHROPIC_API_KEY=<real>
    is NOT overwritten by the file's value, even though the file
    'configures' the same key. Critical for production deployments
    where container secrets are authoritative."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shell-wins")
    f = tmp_path / "creds"
    f.write_text("ANTHROPIC_API_KEY=sk-file-loses\n", encoding="utf-8")
    applied = _secrets.load_credentials(path=f, quiet=True)
    assert applied == {}  # nothing applied — shell already had it
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-shell-wins"


def test_load_credentials_skips_blank_lines_and_comments(tmp_path, monkeypatch):
    """The file format permits comments + blank lines so users can
    annotate their setup. Both forms ignored."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / "creds"
    f.write_text(
        "# This is the API key for nightly auto-curate runs.\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-real\n"
        "\n"
        "# Future setting:\n"
        "# MOXFIELD_USER=alice\n",
        encoding="utf-8",
    )
    applied = _secrets.load_credentials(path=f, quiet=True)
    assert applied == {"ANTHROPIC_API_KEY": "sk-real"}


def test_load_credentials_trims_whitespace_around_key_and_value(
    tmp_path, monkeypatch,
):
    """Casual editing lands keys/values with surrounding spaces. Trim."""
    monkeypatch.delenv("MYKEY", raising=False)
    f = tmp_path / "creds"
    f.write_text("   MYKEY   =   value here   \n", encoding="utf-8")
    _secrets.load_credentials(path=f, quiet=True)
    assert os.environ["MYKEY"] == "value here"


def test_load_credentials_strips_wrapping_quotes(tmp_path, monkeypatch):
    """Users copy-pasting from .env examples often leave the quotes
    in. Strip a single matched pair so the value behaves the same
    whether or not the user remembers to unquote."""
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.delenv("SINGLE", raising=False)
    f = tmp_path / "creds"
    f.write_text(
        'QUOTED="sk-ant-quoted"\n'
        "SINGLE='sk-ant-single'\n",
        encoding="utf-8",
    )
    _secrets.load_credentials(path=f, quiet=True)
    assert os.environ["QUOTED"] == "sk-ant-quoted"
    assert os.environ["SINGLE"] == "sk-ant-single"


def test_load_credentials_drops_empty_values(tmp_path, monkeypatch):
    """``KEY=`` with no value is treated as 'not configured', not as
    'set to empty string'. Defends against accidentally overwriting
    a real env var with an empty value."""
    monkeypatch.delenv("EMPTY", raising=False)
    f = tmp_path / "creds"
    f.write_text("EMPTY=\nREAL=value\n", encoding="utf-8")
    applied = _secrets.load_credentials(path=f, quiet=True)
    assert "EMPTY" not in applied
    assert "EMPTY" not in os.environ
    assert applied["REAL"] == "value"


def test_load_credentials_skips_malformed_lines(tmp_path, monkeypatch, capsys):
    """Lines without ``=`` are skipped with a warning on stderr — don't
    silently swallow them (could mask a typo) and don't crash."""
    monkeypatch.delenv("OK", raising=False)
    f = tmp_path / "creds"
    f.write_text(
        "OK=fine\n"
        "this_line_has_no_equals_sign\n"
        "ALSO=fine\n",
        encoding="utf-8",
    )
    _secrets.load_credentials(path=f, quiet=True)
    err = capsys.readouterr().err
    assert "this_line_has_no_equals_sign" in err
    assert os.environ["OK"] == "fine"
    assert os.environ["ALSO"] == "fine"


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_load_credentials_missing_file_returns_empty(tmp_path, monkeypatch):
    """No file → empty dict, no crash. The user might be relying on
    shell env exclusively (production deployment) so the absence is
    a normal case, not an error."""
    nonexistent = tmp_path / "does_not_exist"
    applied = _secrets.load_credentials(path=nonexistent, quiet=True)
    assert applied == {}


def test_load_credentials_missing_file_prints_hint_unless_quiet(
    tmp_path, monkeypatch, capsys,
):
    """First-run users see a stderr hint pointing them to the setup
    command. ``quiet=True`` suppresses it for automated contexts."""
    nonexistent = tmp_path / "does_not_exist"
    _secrets.load_credentials(path=nonexistent, quiet=False)
    err = capsys.readouterr().err
    assert "No credentials file" in err
    assert "commander-config init" in err

    # Quiet mode suppresses the hint.
    _secrets._loaded = False  # bypass the idempotency guard
    capsys.readouterr()
    _secrets.load_credentials(path=nonexistent, quiet=True)
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_load_credentials_is_idempotent(tmp_path, monkeypatch):
    """Calling load twice in one process only loads once (avoids
    re-parsing the file on every CLI invocation chain). ``force=True``
    bypasses the guard for tests."""
    monkeypatch.delenv("ONCE", raising=False)
    f = tmp_path / "creds"
    f.write_text("ONCE=first\n", encoding="utf-8")
    _secrets.load_credentials(path=f, quiet=True)
    assert os.environ["ONCE"] == "first"

    # Mutate the file: second call should NOT re-parse.
    f.write_text("ONCE=second\n", encoding="utf-8")
    _secrets.load_credentials(path=f, quiet=True)
    assert os.environ["ONCE"] == "first"  # unchanged

    # Force=True re-parses.
    monkeypatch.delenv("ONCE", raising=False)
    _secrets.load_credentials(path=f, quiet=True, force=True)
    assert os.environ["ONCE"] == "second"


# ---------------------------------------------------------------------------
# Template scaffolder
# ---------------------------------------------------------------------------

def test_write_credentials_template_creates_directory_and_file(tmp_path):
    """init writes a commented template at the configured path,
    creating parent directories as needed."""
    target = tmp_path / "deep" / "nested" / "creds"
    written = _secrets.write_credentials_template(target)
    assert written == target
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "# commander-builder credentials" in text
    assert "ANTHROPIC_API_KEY" in text  # mentioned in template comment
    assert "commented" not in text or "# ANTHROPIC_API_KEY" in text  # comment, not real


def test_write_credentials_template_refuses_to_overwrite(tmp_path):
    """init MUST NOT overwrite an existing file — that would silently
    destroy whatever keys the user already configured."""
    target = tmp_path / "creds"
    target.write_text("ANTHROPIC_API_KEY=user-set\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _secrets.write_credentials_template(target)
    # Original content preserved.
    assert "user-set" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# config_main CLI
# ---------------------------------------------------------------------------

def test_config_main_path_prints_active_path(monkeypatch, tmp_path, capsys):
    """``commander-config path`` prints just the file path so it's
    shell-scriptable (e.g. ``code $(commander-config path)``)."""
    monkeypatch.setenv(
        _secrets._OVERRIDE_ENV_VAR, str(tmp_path / "creds"),
    )
    rc = _secrets.config_main(["path"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "creds")


def test_config_main_init_creates_template(monkeypatch, tmp_path, capsys):
    """``commander-config init`` creates the template + prints next steps."""
    target = tmp_path / "creds"
    monkeypatch.setenv(_secrets._OVERRIDE_ENV_VAR, str(target))
    rc = _secrets.config_main(["init"])
    assert rc == 0
    assert target.exists()
    out = capsys.readouterr().out
    assert "Wrote template" in out
    assert "Next steps" in out


def test_config_main_init_refuses_existing(monkeypatch, tmp_path, capsys):
    """init on an existing file → rc 1 with error to stderr. Keep the
    user's keys intact."""
    target = tmp_path / "creds"
    target.write_text("ANTHROPIC_API_KEY=existing\n", encoding="utf-8")
    monkeypatch.setenv(_secrets._OVERRIDE_ENV_VAR, str(target))
    rc = _secrets.config_main(["init"])
    assert rc == 1
    assert "existing" in target.read_text(encoding="utf-8")


def test_config_main_show_redacts_values(monkeypatch, tmp_path, capsys):
    """``commander-config show`` reports KEY presence but never prints
    the value (a key check shouldn't make a leak easier)."""
    target = tmp_path / "creds"
    target.write_text(
        "ANTHROPIC_API_KEY=sk-ant-secret-DO-NOT-LEAK\n", encoding="utf-8",
    )
    monkeypatch.setenv(_secrets._OVERRIDE_ENV_VAR, str(target))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = _secrets.config_main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out
    # The actual secret value MUST NOT appear in show output.
    assert "sk-ant-secret-DO-NOT-LEAK" not in out


def test_config_main_show_no_file(monkeypatch, tmp_path, capsys):
    """show on a non-existent file is a friendly nudge, not an error."""
    target = tmp_path / "missing"
    monkeypatch.setenv(_secrets._OVERRIDE_ENV_VAR, str(target))
    rc = _secrets.config_main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "commander-config init" in out
