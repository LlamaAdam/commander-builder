"""Tests for scripts/backfill_web_margins.py — the one-off signed-margin
backfill for web-saved knowledge-log rows.

Builds a temp knowledge_log with explicit row ids straddling the id-314
seat-attribution fence, then checks:

  * dry-run reports the changes without writing anything;
  * --apply rewrites exactly the recognized, wrong rows;
  * rows below the fence are NEVER touched even when their margin is
    provably wrong (recomputing pre-fix rows would launder artifacts);
  * decisive == 0 rows land margin NULL (matching the fixed writer);
  * AB-shaped rows (wins_a/wins_b) and unparseable sim_reports are
    skipped untouched.

No network, no Forge — pure SQLite in tmp_path.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_web_margins as bwm  # noqa: E402

from commander_builder.knowledge_log import init_db  # noqa: E402


def _insert_row(db: Path, row_id: int, margin, sim_report) -> None:
    """Insert one iterations row with an EXPLICIT id (the fence is
    id-based, so the fixture must control ids exactly)."""
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "INSERT INTO iterations (id, deck_id, deck_name, bracket, "
            "verdict, margin, sim_report, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id, "deck", "Deck", 3, "reverted", margin,
                json.dumps(sim_report) if sim_report is not None else None,
                "2026-08-01T00:00:00+00:00",
            ),
        )
        conn.commit()


def _margin_of(db: Path, row_id: int):
    with closing(sqlite3.connect(str(db))) as conn:
        return conn.execute(
            "SELECT margin FROM iterations WHERE id = ?", (row_id,),
        ).fetchone()[0]


@pytest.fixture
def seeded_db(tmp_path) -> Path:
    """A knowledge_log with rows on both sides of the id-314 fence."""
    db = tmp_path / "backfill_klog.sqlite"
    init_db(db)
    # PRE-FENCE row (id 100): margin is provably wrong for its report
    # (abs 8 for a 12-4 old-side win) — but seat attribution was broken
    # when it was written, so it must stay untouched.
    _insert_row(db, 100, 8, {"old_wins": 12, "new_wins": 4, "draws": 0,
                             "total_games": 20, "margin": 8})
    # POST-FENCE regression row (id 320): pre-signed-margin-fix web save,
    # margin stored as abs(new - old) = 8; must become -8.
    _insert_row(db, 320, 8, {"old_wins": 12, "new_wins": 4, "draws": 0,
                             "total_games": 20, "margin": 8})
    # POST-FENCE zero-decisive row (id 321): fabricated 0 → NULL.
    _insert_row(db, 321, 0, {"old_wins": 0, "new_wins": 0, "draws": 4,
                             "total_games": 4, "margin": 0})
    # POST-FENCE AB-shaped row (id 322): auto-curate always stored signed
    # margins; not compare-shaped → skipped untouched.
    _insert_row(db, 322, -3, {"wins_a": 5, "wins_b": 2, "games": 10})
    # POST-FENCE already-correct row (id 323): idempotence no-op.
    _insert_row(db, 323, -8, {"old_wins": 12, "new_wins": 4, "draws": 0,
                              "total_games": 20, "margin": 8})
    # POST-FENCE row with no sim_report (id 324): skipped.
    _insert_row(db, 324, None, None)
    return db


def test_dry_run_reports_but_does_not_write(seeded_db):
    summary = bwm.backfill(seeded_db, apply=False)
    assert summary["applied"] is False
    changed_ids = {c["id"] for c in summary["changes"]}
    assert changed_ids == {320, 321}
    # Nothing written.
    assert _margin_of(seeded_db, 320) == 8
    assert _margin_of(seeded_db, 321) == 0
    assert _margin_of(seeded_db, 100) == 8


def test_apply_rewrites_only_recognized_wrong_rows(seeded_db):
    summary = bwm.backfill(seeded_db, apply=True)
    assert summary["applied"] is True
    assert {c["id"] for c in summary["changes"]} == {320, 321}
    # Regression row: abs 8 → signed -8.
    assert _margin_of(seeded_db, 320) == -8
    # Zero-decisive row: fabricated 0 → NULL.
    assert _margin_of(seeded_db, 321) is None
    # AB-shaped, already-correct, and empty rows untouched.
    assert _margin_of(seeded_db, 322) == -3
    assert _margin_of(seeded_db, 323) == -8
    assert _margin_of(seeded_db, 324) is None


def test_fence_row_never_touched_even_when_wrong(seeded_db):
    """id 100 < 314 carries the same provably-wrong abs margin as id 320
    — the fence must exclude it from scan AND write."""
    summary = bwm.backfill(seeded_db, apply=True)
    assert all(c["id"] >= bwm.MIN_ROW_ID for c in summary["changes"])
    assert _margin_of(seeded_db, 100) == 8


def test_apply_is_idempotent(seeded_db):
    bwm.backfill(seeded_db, apply=True)
    second = bwm.backfill(seeded_db, apply=True)
    assert second["changes"] == []
    assert second["unchanged"] >= 2  # 320 + 321 now correct, 323 still


def test_recompute_margin_shapes():
    assert bwm.recompute_margin({"old_wins": 12, "new_wins": 4}) == (True, -8)
    assert bwm.recompute_margin({"old_wins": 4, "new_wins": 12}) == (True, 8)
    assert bwm.recompute_margin({"old_wins": 0, "new_wins": 0}) == (True, None)
    assert bwm.recompute_margin({"wins_a": 5, "wins_b": 2}) == (False, None)
    assert bwm.recompute_margin(None) == (False, None)
    assert bwm.recompute_margin({"old_wins": "junk", "new_wins": 3}) == (False, None)


def test_main_dry_run_prints_table(seeded_db, capsys):
    rc = bwm.main(["--db", str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "320" in out and "321" in out
    assert "NULL" in out           # 321's after-value renders as NULL
    assert "314" in out            # fence stated in the report
    # Still nothing written.
    assert _margin_of(seeded_db, 320) == 8


def test_main_errors_on_missing_db(tmp_path, capsys):
    rc = bwm.main(["--db", str(tmp_path / "nope.sqlite")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err
