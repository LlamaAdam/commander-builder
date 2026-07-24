"""Tests for scripts/soak_pool.py's per-sim row recording.

Focused on the gauntlet recorder's status semantics: a
'loop_unattributed' GauntletResult (batch cut short by a looping game
that no seat could be credited for — Forge prints the game log only
after a game completes, so a hung game leaves no Turn line to
attribute) is an honest SHORT row whose completed games are real data.
It must count toward sims_done/games_done like a 'done' row, not be
booked as a failure, and the JSONL row must carry the distinct status
verbatim so downstream consumers (margin_analysis, merge_soak) can tell
a legitimately short row from a genuine error.

No Forge, no threads: `_record_gauntlet` is exercised on a minimal
stand-in `self` (it only touches the lock, the counters, and args).
"""
from __future__ import annotations

import json
import threading
import types
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import soak_pool  # noqa: E402

from commander_builder.forge_batch import GauntletResult  # noqa: E402


def _soak_stub(tmp_path: Path):
    """Minimal object satisfying everything _record_gauntlet reads."""
    s = types.SimpleNamespace()
    s.lock = threading.Lock()
    s.sims_done = 0
    s.sims_failed = 0
    s.games_done = 0
    s.wins_a = 0
    s.wins_b = 0
    s.args = types.SimpleNamespace(label="testhost",
                                   out=tmp_path / "rows.jsonl")
    s.args.out.write_text("", encoding="utf-8")
    return s


def _read_rows(out: Path) -> list[dict]:
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_record_gauntlet_counts_loop_unattributed_as_completed(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D v2 [B3].dck",
        gauntlet=["G1.dck", "G2.dck", "G3.dck"],
        games=17, wins=5, losses=11, draws=1,
        status="loop_unattributed",
        error=("loop at game 18: no seat attributable from partial stdout "
               "(Forge prints the game log only after a game completes); "
               "kept 17 completed games"),
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D v2 [B3].dck")

    assert s.sims_done == 1          # counted as data, NOT a failure
    assert s.sims_failed == 0
    assert s.games_done == 17        # only the completed games
    assert s.wins_a == 5 and s.wins_b == 11

    (row,) = _read_rows(s.args.out)
    assert row["status"] == "loop_unattributed"   # distinct status, verbatim
    assert row["games"] == 17
    assert row["role"] == "v2"
    assert "kept 17 completed games" in row["error"]


def test_record_gauntlet_still_books_real_failures_as_failed(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D [B3].dck",
        games=2, wins=1, losses=1,
        status="failed", error="Forge exited with code 1",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck")

    assert s.sims_done == 0
    assert s.sims_failed == 1
    assert s.games_done == 0

    (row,) = _read_rows(s.args.out)
    assert row["status"] == "failed"


def test_record_gauntlet_done_row_unchanged(tmp_path):
    s = _soak_stub(tmp_path)
    res = GauntletResult(
        test_deck="[USER] D [B3].dck",
        games=40, wins=12, losses=26, draws=2, status="done",
    )
    soak_pool.Soak._record_gauntlet(
        s, res, None, tmp_path / "[USER] D [B3].dck")

    assert s.sims_done == 1 and s.games_done == 40
    (row,) = _read_rows(s.args.out)
    assert row["status"] == "done" and row["role"] == "base"
