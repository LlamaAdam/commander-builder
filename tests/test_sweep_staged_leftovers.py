"""Tests for scripts/sweep_staged_leftovers.py — leak recovery tool.

Filenames use REAL-shaped bracketed stems on purpose: the whole reason
this tool exists is that stem-based globs (pathlib AND shells) treat
[USER]/[B3] square brackets as character classes and miss the files.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "sweep_staged_leftovers",
    REPO_ROOT / "scripts" / "sweep_staged_leftovers.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)

LEAKED = [
    "[USER] Cleanup Deck [B3]__tier3_base.dck",
    "[USER] Cleanup Deck [B3]__tier3_score.dck",
    "[PREMADE] Some Deck [B3]__perswap_00_top.dck",
]
KEPT = [
    "[USER] Cleanup Deck [B3].dck",      # the real deck
    "[USER] Cleanup Deck [B3] v2.dck",   # a real variant
    "notes__tier3_x.txt",                # wrong extension
]


def seed(deck_dir):
    deck_dir.mkdir()
    for name in LEAKED + KEPT:
        (deck_dir / name).write_text("x", encoding="utf-8")


def test_dry_run_is_the_default_and_deletes_nothing(tmp_path, capsys):
    deck_dir = tmp_path / "decks"
    seed(deck_dir)
    rc = sweep.main([str(deck_dir)])
    assert rc == 0
    assert sorted(os.listdir(deck_dir)) == sorted(LEAKED + KEPT)
    out = capsys.readouterr().out
    assert "3 staged leftover(s)" in out
    assert "dry run" in out and "--delete" in out
    for name in LEAKED:
        assert f"would delete {name}" in out


def test_delete_removes_only_staged_leftovers(tmp_path, capsys):
    deck_dir = tmp_path / "decks"
    seed(deck_dir)
    rc = sweep.main([str(deck_dir), "--delete"])
    assert rc == 0
    assert sorted(os.listdir(deck_dir)) == sorted(KEPT)
    out = capsys.readouterr().out
    assert "3 staged leftover(s)" in out
    assert "dry run" not in out


def test_find_leftovers_handles_brackets_and_ignores_non_dck(tmp_path):
    deck_dir = tmp_path / "decks"
    seed(deck_dir)
    hits = sweep.find_leftovers(deck_dir)
    assert sorted(p.name for p in hits) == sorted(LEAKED)


def test_missing_directory_errors(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        sweep.main([str(tmp_path / "nope")])
    assert exc.value.code == 2
    assert "no such directory" in capsys.readouterr().err
