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


# --- --era-boundary-report (R2-D5) -----------------------------------------
#
# The era-3/4 boundary is a bare date cut at 2026-08-14, while the two
# other ambiguous windows get the NULL-not-guess treatment. The 08-14
# fixes were COMMITS, not midnight cutovers, so a row written that
# morning carries an old margin-threshold verdict and is nonetheless
# stamped era 4 and admitted to the FP-013 training floor. Whether such
# rows exist is a fact about the owner's machine — this mode lists them
# and writes NOTHING.


def _insert_dated_row(db: Path, row_id: int, created_at: str,
                      verdict: str = "kept", era=None) -> None:
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "INSERT INTO iterations (id, deck_id, deck_name, bracket, "
            "verdict, created_at, measurement_era) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, "deck", "Deck", 3, verdict, created_at, era),
        )
        conn.commit()


@pytest.fixture
def era_db(tmp_path) -> Path:
    """Rows on the boundary day plus one either side of it."""
    db = tmp_path / "era_klog.sqlite"
    init_db(db)
    _insert_dated_row(db, 400, "2026-08-13T22:10:00+00:00", "kept", era=3)
    _insert_dated_row(db, 401, "2026-08-14T08:15:00+00:00", "kept", era=4)
    _insert_dated_row(db, 402, "2026-08-14T19:40:00+00:00", "reverted", era=4)
    _insert_dated_row(db, 403, "2026-08-15T09:00:00+00:00", "neutral", era=4)
    return db


def test_era_report_lists_only_the_boundary_day(era_db):
    report = bwm.era_boundary_report(era_db)
    assert report["boundary_date"] == "2026-08-14"
    assert report["shifted_start"] == "2026-08-15"
    assert [r["id"] for r in report["rows"]] == [401, 402]


def test_era_report_carries_the_five_columns_the_owner_needs(era_db):
    rows = bwm.era_boundary_report(era_db)["rows"]
    first = rows[0]
    assert first["id"] == 401
    assert first["created_at"] == "2026-08-14T08:15:00+00:00"
    assert first["verdict"] == "kept"
    assert first["era"] == 4                 # the STORED stamp today
    assert first["era_if_shifted"] == 3      # ...and under a moved boundary
    assert first["side"] == "unknown"        # no --commit-time given


def test_era_report_splits_rows_once_the_commit_time_is_known(era_db):
    rows = bwm.era_boundary_report(era_db, commit_time="14:00")["rows"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[401]["side"].startswith("before")   # 08:15 — pre-commit
    assert by_id[402]["side"].startswith("after")    # 19:40 — post-commit


def test_era_report_writes_nothing(era_db):
    """Report-only means report-only: every row's stored era, verdict and
    margin survive the call byte-for-byte."""
    def snapshot():
        with closing(sqlite3.connect(str(era_db))) as conn:
            return conn.execute(
                "SELECT id, verdict, measurement_era, margin, created_at "
                "FROM iterations ORDER BY id"
            ).fetchall()

    before = snapshot()
    bwm.era_boundary_report(era_db, commit_time="14:00")
    assert snapshot() == before


def test_era_report_reuses_the_library_era_function(era_db, monkeypatch):
    """``era_if_shifted`` must come from ``measurement_era_for`` itself,
    not a second copy of the boundary rules that can drift from it."""
    calls: list = []
    real = bwm.measurement_era_for

    def spy(created_at, iteration_id=None, **kw):
        calls.append(kw)
        return real(created_at, iteration_id, **kw)

    monkeypatch.setattr(bwm, "measurement_era_for", spy)
    bwm.era_boundary_report(era_db)
    assert calls
    assert all(kw["significance_start"] == "2026-08-15" for kw in calls)


def test_main_era_report_prints_a_labeled_listing(era_db, capsys):
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ERA BOUNDARY REPORT ONLY (no writes" in out
    assert "R2-D5" in out
    assert "401" in out and "402" in out
    assert "403" not in out               # 08-15 is not on the boundary day
    assert "--commit-time" in out         # tells the owner how to split


def test_main_era_report_says_so_when_the_window_is_empty(tmp_path, capsys):
    db = tmp_path / "empty.sqlite"
    init_db(db)
    _insert_dated_row(db, 500, "2026-08-16T09:00:00+00:00")
    rc = bwm.main(["--db", str(db), "--era-boundary-report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NO rows created on 2026-08-14" in out
    assert "leave" in out and "_SIGNIFICANCE_START" in out


def test_main_era_report_refuses_to_ride_along_with_apply(era_db, capsys):
    """The report has no write mode; --apply beside it is a mistake worth
    refusing rather than silently ignoring."""
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report", "--apply"])
    assert rc == 2
    assert "report-only" in capsys.readouterr().err
    # ...and the margin backfill did not run either.
    assert _margin_of(era_db, 401) is None


@pytest.mark.parametrize("bad", ["14h00", "1400", "25:00", "14:60", "noon"])
def test_malformed_commit_time_is_refused_not_guessed(era_db, capsys, bad):
    """The side column is a lexical compare against the ISO time field, so
    a malformed value would quietly sort every row onto one side."""
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report",
                   "--commit-time", bad])
    assert rc == 2
    assert "HH:MM" in capsys.readouterr().err


@pytest.mark.parametrize("good", ["00:00", "14:00", "23:59", "14:32:07"])
def test_well_formed_commit_times_are_accepted(era_db, capsys, good):
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report",
                   "--commit-time", good])
    assert rc == 0
    assert "unknown" not in capsys.readouterr().out


def test_commit_time_without_the_report_flag_is_refused(era_db, capsys):
    rc = bwm.main(["--db", str(era_db), "--commit-time", "14:00"])
    assert rc == 2
    assert "only applies to" in capsys.readouterr().err


def test_era_report_help_is_discoverable(capsys):
    with pytest.raises(SystemExit):
        bwm.main(["--help"])
    out = capsys.readouterr().out
    assert "--era-boundary-report" in out
    assert "REPORT ONLY" in out
