"""``commander`` — one front door for the ~30 console scripts (decision B1).

WHY THIS EXISTS
===============
``[project.scripts]`` has grown to 28 entry points with overlapping verbs
(``commander-import`` vs ``commander-bulk-import`` vs ``commander-mint-v2``;
``commander-curate`` vs ``commander-auto-curate``). A negative-mode review
(P10) called the surface *unholdable*: nothing tells an operator which of
30 hyphenated names to reach for, and ``commander-<TAB>`` is a wall rather
than a menu. This module is the menu — ONE command whose grouped ``--help``
is the map of the whole tool.

WHAT IT IS NOT
==============
It is **not a migration**. Every existing console script stays registered
and keeps working exactly as before; ``commander improve X`` and
``commander-improve X`` are the same call. Nothing here re-parses,
re-orders, or validates arguments — dispatch resolves the target module's
entry function and forwards ``argv`` verbatim, so each subcommand's own
argparse (and therefore its own ``--help``, its own error text, and its own
exit codes) remains the single source of truth. A wrapper that "helpfully"
re-parsed would immediately drift from 28 CLIs it doesn't own.

The registry below is also a contract: ``tests/test_cli.py`` fails when a
``[project.scripts]`` entry has no subcommand (or vice versa), so adding a
script without wiring it into the map is caught at test time rather than
discovered by an operator who can't find it.

Public API
----------
``COMMANDS``   — the subcommand registry (name → script/target/group).
``resolve(name)`` — import the target module, return its entry function.
``render_help()`` — the grouped help text.
``main(argv)`` — the ``commander`` entry point.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Callable, Optional

#: Task areas, in the order they appear in ``commander --help``, with the
#: one-line gloss printed as the group header. Ordered roughly by the
#: lifecycle an operator walks: get a deck, measure it, read the results,
#: keep the environment healthy.
GROUPS: tuple[tuple[str, str], ...] = (
    ("build", "assemble a deck from scratch"),
    ("import", "get decks in and out of the local library"),
    ("sim", "Forge simulation, the empirical loop (needs Java + the Forge jar)"),
    ("analyze", "read-only analysis and reference data"),
    ("web", "the browser / desktop UI"),
    ("maintenance", "setup, environment, and stored state"),
)


@dataclass(frozen=True)
class Command:
    """One subcommand of ``commander``.

    ``script`` is the standalone console script this aliases — kept in the
    registry (not derived) because the tripwire test compares it against
    ``[project.scripts]`` and because ``commander --help`` shows operators
    the equivalence explicitly.

    ``target`` is ``"<module>:<attr>"`` relative to ``commander_builder``,
    matching the value in ``pyproject.toml`` minus the package prefix.
    """
    name: str
    script: str
    target: str
    group: str
    summary: str


# Subcommand names are the script name minus the ``commander-`` prefix —
# mechanical, so an operator who knows one form knows the other. That does
# leave the two ``commander-builder-*`` scripts as ``builder-desktop`` /
# ``builder-bootstrap``; renaming them here would break exactly the
# 1:1 promise this table is supposed to keep.
#
# Summaries are lifted from each target module's own docstring / argparse
# description — this table describes what the code says it does, it does
# not invent a new story for it.
COMMANDS: tuple[Command, ...] = (
    # -- build ------------------------------------------------------------
    Command("build", "commander-build", "deck_builder:main", "build",
            "Assemble a from-scratch deck for a commander (EDHREC-seeded legal 99)."),
    Command("mint-v2", "commander-mint-v2", "premade_mint:main", "build",
            "Mint a heuristic (free, no-LLM) v2 for every [PREMADE] deck without one."),
    # -- import -----------------------------------------------------------
    Command("import", "commander-import", "moxfield_import:main", "import",
            "Pull Moxfield decks into Forge .dck files; bracket harvest and "
            "[PREMADE] pulls."),
    Command("bulk-import", "commander-bulk-import", "moxfield_import:bulk_main",
            "import",
            "Import many decks from a file (or stdin) of one URL per line."),
    Command("push", "commander-push", "moxfield_push:main", "import",
            "Push a local .dck back to Moxfield (clipboard + browser paste path)."),
    # -- sim --------------------------------------------------------------
    Command("snapshot", "commander-snapshot", "snapshot_deck:main", "sim",
            "Snapshot a .dck to a versioned filename so v1/v2 both exist on disk."),
    Command("curate", "commander-curate", "pool_curator:main", "sim",
            "Curate the canonical opponent pool for a bracket by round-robin self-play."),
    Command("match", "commander-match", "run_match:main", "sim",
            "Run a user deck against the curated pool and report its weaknesses."),
    Command("compare", "commander-compare", "compare_versions:main", "sim",
            "Head-to-head A/B sim of two deck versions at the same table."),
    Command("iterate", "commander-iterate", "iteration_loop:main", "sim",
            "One iteration cycle: snapshot -> apply manifest -> sim -> verdict -> log."),
    Command("auto-curate", "commander-auto-curate", "proposer:auto_curate_main", "sim",
            "Advisor -> Claude curator -> apply -> optional A/B sim, in one go."),
    Command("improve", "commander-improve", "improve:improve_main", "sim",
            "Greedy improve loop: a round advances only on a `kept` A/B verdict."),
    # -- analyze ----------------------------------------------------------
    Command("advise", "commander-advise", "improvement_advisor:main", "analyze",
            "Suggest swaps for a deck without a browser-Claude session."),
    Command("top", "commander-top", "edhrec_client:top_main", "analyze",
            "List EDHREC's most-played cards for a window / card type."),
    Command("combos", "commander-combos", "combo_detection:main", "analyze",
            "Detect infinite combos in a deck, or refresh the combo DB."),
    Command("meta-test", "commander-meta-test", "meta_test:main", "analyze",
            "Pit your deck against canonical reference builds at a bracket."),
    Command("corpus-themes", "commander-corpus-themes", "corpus_themes:main", "analyze",
            "Mine the on-disk deck corpus into per-cluster empirical norms."),
    Command("tournament", "commander-tournament", "edhtop16_client:main", "analyze",
            "Import cEDH tournament results from edhtop16.com (bracket-5, exploratory)."),
    Command("history", "commander-history", "report:main", "analyze",
            "Render a deck's iteration lineage as a Markdown report."),
    Command("judge", "commander-judge", "deck_judge:main", "analyze",
            "Blinded LLM panel on two decks' construction (an OPINION; "
            "Forge decides which deck is better)."),
    Command("adopt", "commander-adopt", "adopt:main", "analyze",
            "Understand an imported deck (primer vs. list) and get small "
            "preference-steered swap suggestions (polish-capped)."),
    # -- web --------------------------------------------------------------
    Command("builder-desktop", "commander-builder-desktop", "desktop:main", "web",
            "Run Commander Builder as a native desktop window."),
    # -- maintenance ------------------------------------------------------
    Command("init", "commander-init", "init_cli:main", "maintenance",
            "Guided first-run setup: dependencies -> oracle cache -> decks -> pool."),
    Command("builder-bootstrap", "commander-builder-bootstrap", "bootstrap:main",
            "maintenance",
            "Check / fetch the external dependencies (Forge jar, JRE, mtg_cards)."),
    Command("doctor", "commander-doctor", "doctor:main", "maintenance",
            "Environment health check; exits non-zero if any RED check fails."),
    Command("status", "commander-status", "status:main", "maintenance",
            "Deck listing, or one deck's at-a-glance dashboard."),
    Command("config", "commander-config", "_secrets:config_main", "maintenance",
            "Inspect / scaffold the out-of-repo credentials file."),
    Command("oracle-refresh", "commander-oracle-refresh", "oracle_store:main",
            "maintenance",
            "Report / rewrite oracle-snapshot drift; bulk-populate from Scryfall."),
    Command("export", "commander-export", "export:main", "maintenance",
            "Export / import the knowledge log as JSON."),
    Command("revert", "commander-revert", "revert_to:main", "maintenance",
            "Roll back a deck to a logged iteration (live deck is backed up first)."),
    Command("local-model", "commander-local-model", "local_model:main", "maintenance",
            "Preflight the local-model daemon, or measure its agreement rate."),
)

#: Name → Command. Built once; ``COMMANDS`` is the editable source of truth.
BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}

#: The umbrella's own script name. Excluded from the script↔subcommand
#: tripwire: ``commander`` is the multiplexer, not one of the things
#: multiplexed.
UMBRELLA_SCRIPT = "commander"


def resolve(name: str) -> Callable[..., Optional[int]]:
    """Import ``name``'s target module and return its entry function.

    Imports lazily and one-module-at-a-time on purpose: ``commander
    --help`` must stay instant, and importing every target would drag in
    Flask, the EDHREC client, and the Forge runner just to print a menu.
    """
    cmd = BY_NAME[name]
    mod_name, _, attr = cmd.target.partition(":")
    module = importlib.import_module(f"commander_builder.{mod_name}")
    return getattr(module, attr)


def render_help() -> str:
    """The grouped menu printed by ``commander`` / ``commander --help``."""
    lines = [
        "commander — one front door for the commander-builder CLI.",
        "",
        "Usage:",
        "  commander <command> [args...]      run a command",
        "  commander <command> --help         that command's own help",
        "",
        "Every command below is ALSO installed as a standalone script:",
        "  `commander improve ...` == `commander-improve ...`. Same code, "
        "same flags,",
        "  same exit codes — the umbrella only forwards arguments.",
    ]
    width = max(len(c.name) for c in COMMANDS)
    for group, gloss in GROUPS:
        members = [c for c in COMMANDS if c.group == group]
        if not members:
            continue
        lines.append("")
        lines.append(f"{group} — {gloss}")
        for cmd in members:
            lines.append(f"  {cmd.name.ljust(width)}  {cmd.summary}")
    lines.append("")
    lines.append("New here? Start with `commander init` (guided first-run setup).")
    return "\n".join(lines)


def _unknown(name: str) -> str:
    """Error text for an unrecognized subcommand.

    Lists every valid name rather than guessing at a "did you mean" —
    the grouped list is short enough to print and is strictly more useful
    than one fuzzy suggestion when the operator's problem is usually "I
    don't know what this thing is called".
    """
    lines = [f"commander: unknown command {name!r}", "", "Valid commands:"]
    for group, _gloss in GROUPS:
        members = [c.name for c in COMMANDS if c.group == group]
        if members:
            lines.append(f"  {group:12s} {' '.join(members)}")
    lines.append("")
    lines.append("Run `commander --help` for one-line descriptions.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``commander`` — dispatch to a subcommand's entry point.

    Returns the target's exit code verbatim (``None`` → 0, the argparse
    convention every entry point here already follows). ``SystemExit`` is
    deliberately NOT caught: argparse raises it for ``--help`` and for bad
    arguments, and swallowing it would change the exit codes the
    standalone scripts produce.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    # Bare `commander`, `commander -h`, `commander help`: the menu. Exit 0
    # — asking for the map is not an error, and an operator running
    # `commander` to see what's there shouldn't get a failure code.
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(render_help())
        return 0

    name, rest = argv[0], argv[1:]
    if name not in BY_NAME:
        print(_unknown(name), file=sys.stderr)
        return 2

    entry = resolve(name)
    rc = entry(rest)
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
