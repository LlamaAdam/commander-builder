"""Tests for the ``commander`` umbrella multiplexer (decision B1).

The load-bearing test in here is
``test_every_console_script_has_a_subcommand`` — a tripwire that fails
when someone adds a ``[project.scripts]`` entry without wiring it into
``cli.COMMANDS`` (or removes one and leaves the subcommand behind). The
umbrella's whole value is being the complete map of the CLI surface; a
map that silently goes stale is worse than no map.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from commander_builder import cli

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"


#: One ``name = "module:attr"`` line inside ``[project.scripts]``.
_SCRIPT_LINE = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]+)"\s*$')


def _project_scripts() -> dict[str, str]:
    """``{script_name: "module:attr"}`` read out of ``[project.scripts]``.

    Deliberately a line match, NOT ``tomllib``: ``tomllib`` is stdlib only
    from 3.11, the project supports 3.10 (``requires-python``), CI runs a
    3.10 lane, and the repo declares no TOML dependency to fall back on —
    a ``tomllib`` import here is a CI break, not a portability nicety
    (it broke tests/test_local_model.py on 2026-08-20). The section's
    format is one simple assignment per line, so a regex is sufficient
    and dependency-free.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    section = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    out: dict[str, str] = {}
    for line in section.splitlines():
        m = _SCRIPT_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    assert out, "[project.scripts] parsed as empty — parser or section drift"
    return out


# --------------------------------------------------------------------------- #
# Registry <-> [project.scripts] tripwire
# --------------------------------------------------------------------------- #
def test_every_console_script_has_a_subcommand():
    scripts = _project_scripts()
    scripts.pop(cli.UMBRELLA_SCRIPT, None)  # the multiplexer itself
    mapped = {c.script for c in cli.COMMANDS}
    missing = sorted(set(scripts) - mapped)
    assert not missing, (
        f"console scripts with no `commander` subcommand: {missing}. "
        "Add them to cli.COMMANDS."
    )


def test_every_subcommand_maps_to_a_registered_script():
    scripts = _project_scripts()
    stale = sorted(c.script for c in cli.COMMANDS if c.script not in scripts)
    assert not stale, (
        f"subcommands aliasing scripts that no longer exist: {stale}"
    )


def test_subcommand_targets_match_the_script_targets():
    """The alias must call the SAME function the script calls."""
    scripts = _project_scripts()
    for cmd in cli.COMMANDS:
        assert scripts[cmd.script] == f"commander_builder.{cmd.target}", (
            f"{cmd.name} dispatches to {cmd.target}, but {cmd.script} is "
            f"registered as {scripts[cmd.script]}"
        )


def test_subcommand_names_strip_the_commander_prefix():
    for cmd in cli.COMMANDS:
        assert cmd.script == f"commander-{cmd.name}"


def test_registry_has_no_duplicate_names_or_scripts():
    names = [c.name for c in cli.COMMANDS]
    scripts = [c.script for c in cli.COMMANDS]
    assert len(names) == len(set(names))
    assert len(scripts) == len(set(scripts))


def test_every_command_belongs_to_a_declared_group():
    declared = {g for g, _ in cli.GROUPS}
    assert {c.group for c in cli.COMMANDS} <= declared


def test_resolve_returns_the_real_entry_function():
    fn = cli.resolve("doctor")
    from commander_builder import doctor
    assert fn is doctor.main


@pytest.mark.parametrize("name", sorted(c.name for c in cli.COMMANDS))
def test_every_subcommand_resolves_to_a_callable(name):
    """Also the import smoke test: every target module still imports."""
    assert callable(cli.resolve(name))


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def test_dispatch_forwards_argv_verbatim(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "resolve", lambda name: fake_main)
    rc = cli.main(["improve", "--rounds", "3", "--deck", "x y", "--", "-n"])
    assert rc == 0
    # Verbatim: same list, same order, nothing consumed or reordered.
    assert seen["argv"] == ["--rounds", "3", "--deck", "x y", "--", "-n"]


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42])
def test_dispatch_returns_exit_code_verbatim(monkeypatch, code):
    monkeypatch.setattr(cli, "resolve", lambda name: lambda argv: code)
    assert cli.main(["curate", "--bracket", "3"]) == code


def test_dispatch_treats_none_return_as_success(monkeypatch):
    monkeypatch.setattr(cli, "resolve", lambda name: lambda argv: None)
    assert cli.main(["doctor"]) == 0


def test_dispatch_lets_systemexit_through(monkeypatch):
    """argparse raises SystemExit for --help and for bad args; catching it
    would change the exit code the standalone script produces."""
    def boom(argv):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "resolve", lambda name: boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshot", "--nope"])
    assert exc.value.code == 2


def test_subcommand_help_shows_the_targets_own_help(capsys):
    """`commander <sub> --help` == `<script> --help` (the target's argparse
    prints and exits)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["oracle-refresh", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "commander-oracle-refresh" in out
    assert "--from-bulk" in out


def test_dispatch_reads_sys_argv_when_argv_is_none(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "resolve", lambda name: fake_main)
    monkeypatch.setattr("sys.argv", ["commander", "status", "--json"])
    assert cli.main() == 0
    assert seen["argv"] == ["--json"]


# --------------------------------------------------------------------------- #
# Help / unknown command
# --------------------------------------------------------------------------- #
def test_no_args_prints_grouped_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for group, _gloss in cli.GROUPS:
        assert f"{group} — " in out
    for cmd in cli.COMMANDS:
        assert f" {cmd.name} " in out or out.rstrip().endswith(f" {cmd.name}")
    assert "commander-improve" in out  # the alias equivalence is stated


@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_flags_print_the_same_menu(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out.strip() == cli.render_help().strip()


def test_help_lists_every_subcommand_with_a_summary(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    for cmd in cli.COMMANDS:
        assert cmd.summary in out


def test_help_is_encodable_by_the_default_windows_console():
    cli.render_help().encode("cp1252")


def test_unknown_subcommand_lists_valid_ones(capsys):
    rc = cli.main(["improove", "--rounds", "3"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown command 'improove'" in err
    assert "improve" in err
    assert "curate" in err


def test_unknown_subcommand_does_not_import_anything(monkeypatch):
    """A typo must not drag in a target module (or run one)."""
    def explode(name):  # pragma: no cover — asserted not to run
        raise AssertionError(f"resolve() called for unknown command {name!r}")

    monkeypatch.setattr(cli, "resolve", explode)
    assert cli.main(["nope"]) == 2
