"""Tests for ``scripts/soak_throughput.py`` — exit-path and --out safety.

Two low-severity regressions pinned here (2026-07):

1. OVERWRITE GUARD: the script truncated ``--out`` unconditionally at
   startup, so re-launching with the same path silently threw away every
   JSONL row from the previous soak. It must now refuse when --out exists
   and is non-empty, unless --force is passed. (Local file-open behavior
   only — nothing about share/publish paths is involved.)

2. ZERO-SIM EXIT: when the budget expired before any sim completed
   (``sims_done == 0``), the final projection print divided by zero and
   the whole run ended in a ZeroDivisionError traceback — after hours of
   soaking. The exit path must survive and print an n/a projection.

No Forge is touched: ``_deck_pairs`` / ``_build_runners`` are
monkeypatched, and ``--hours 0`` makes the batch loop a no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't a package and isn't on sys.path by default; same import
# convention as test_merge_soak.py / test_detune_deck.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import soak_throughput  # noqa: E402


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    """Common argv: zero budget, results routed into tmp_path."""
    return [
        "--hours", "0",
        "--out", str(tmp_path / "rows.jsonl"),
        "--summary", str(tmp_path / "summary.json"),
        *extra,
    ]


def _stub_forge(monkeypatch) -> None:
    """Get main() past the deck/runner discovery without a Forge install."""
    monkeypatch.setattr(
        soak_throughput, "_deck_pairs",
        lambda: [(Path("a.dck"), Path("a v2.dck"))],
    )
    monkeypatch.setattr(
        soak_throughput, "_build_runners", lambda n: [object()],
    )


# --------------------------------------------------------------------------- #
# --out overwrite guard
# --------------------------------------------------------------------------- #

def test_refuses_to_overwrite_nonempty_out_without_force(tmp_path):
    out = tmp_path / "rows.jsonl"
    out.write_text('{"prior": "soak row"}\n', encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        soak_throughput.main(_argv(tmp_path))
    # Refusal message must name the file and the escape hatch.
    assert "rows.jsonl" in str(ei.value)
    assert "--force" in str(ei.value)
    # And crucially: the prior rows survive untouched.
    assert out.read_text(encoding="utf-8") == '{"prior": "soak row"}\n'


def test_force_flag_allows_overwrite(tmp_path, monkeypatch, capsys):
    _stub_forge(monkeypatch)
    out = tmp_path / "rows.jsonl"
    out.write_text('{"prior": "soak row"}\n', encoding="utf-8")
    assert soak_throughput.main(_argv(tmp_path, "--force")) == 0
    # Truncated as requested.
    assert out.read_text(encoding="utf-8") == ""


def test_empty_or_missing_out_needs_no_force(tmp_path, monkeypatch, capsys):
    _stub_forge(monkeypatch)
    # Missing file: fine.
    assert soak_throughput.main(_argv(tmp_path)) == 0
    # Now it exists but is EMPTY (zero rows to protect): still fine.
    assert soak_throughput.main(_argv(tmp_path)) == 0


# --------------------------------------------------------------------------- #
# sims_done == 0 exit path
# --------------------------------------------------------------------------- #

def test_zero_sims_exit_does_not_zerodivide(tmp_path, monkeypatch, capsys):
    """Budget expires before any sim → the DONE/projection prints must
    survive (old code raised ZeroDivisionError right at the finish)."""
    _stub_forge(monkeypatch)
    assert soak_throughput.main(_argv(tmp_path)) == 0
    printed = capsys.readouterr().out
    assert "[soak] DONE: 0 sims" in printed
    assert "projection: n/a" in printed
    # The summary file still lands, with honest null projections.
    assert (tmp_path / "summary.json").exists()
