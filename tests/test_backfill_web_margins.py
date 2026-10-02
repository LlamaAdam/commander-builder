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


# --- R3 C-05 (2026-09-03): the printed instructions actually work ----------
#
# The report told the owner to move _SIGNIFICANCE_START "or NULL the day".
# Moving the constant never relabels a stamped row (the v3 backfill fills
# NULL eras only); NULLing by hand is re-stamped era 4 by the next init_db.
# --apply-era-shift is the write that lasts, and the report prints step 2.

def _eras(db: Path) -> dict:
    with closing(sqlite3.connect(str(db))) as conn:
        return dict(conn.execute(
            "SELECT id, measurement_era FROM iterations ORDER BY id"))


def test_apply_era_shift_relabels_the_day_and_survives_init_db(era_db):
    result = bwm.apply_era_shift(era_db)
    assert sorted(result["changed"]) == [(401, 4, 3), (402, 4, 3)]
    assert _eras(era_db) == {400: 3, 401: 3, 402: 3, 403: 4}
    # The old "NULL the day" advice was undone by the next init_db; a
    # stored stamp is not.
    init_db(era_db)
    init_db(era_db)
    assert _eras(era_db) == {400: 3, 401: 3, 402: 3, 403: 4}
    # Idempotent.
    assert bwm.apply_era_shift(era_db)["changed"] == []


def test_main_apply_era_shift_prints_both_steps(era_db, capsys):
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report",
                   "--apply-era-shift"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--apply-era-shift APPLIED: 2 row(s)" in out
    assert "STEP 2" in out and '_SIGNIFICANCE_START = "2026-08-15"' in out
    assert _eras(era_db)[401] == 3


def test_report_without_the_shift_flag_still_writes_nothing(era_db):
    before = _eras(era_db)
    bwm.main(["--db", str(era_db), "--era-boundary-report"])
    assert _eras(era_db) == before


def test_report_advice_names_both_steps_and_not_null_the_day(era_db, capsys):
    bwm.main(["--db", str(era_db), "--era-boundary-report"])
    out = capsys.readouterr().out
    assert "step 1" in out and "--apply-era-shift" in out
    assert "step 2" in out and "_SIGNIFICANCE_START" in out
    assert "NULL the day" not in out.split("Why both")[0]


def test_apply_era_shift_without_the_report_flag_is_refused(era_db, capsys):
    rc = bwm.main(["--db", str(era_db), "--apply-era-shift"])
    assert rc == 2
    assert "only applies to" in capsys.readouterr().err
    assert _eras(era_db)[401] == 4


# --- R3 C-07 (2026-09-03): UTC row time vs the commit's own offset --------

def test_commit_time_offset_is_honored(era_db):
    """Row 402 is 19:40Z. A commit at 16:00 local in UTC-4 is 20:00Z, so
    the row is BEFORE it; the old lexical compare ('19:40' < '16:00' is
    False) called it after."""
    rows = {r["id"]: r for r in
            bwm.era_boundary_report(era_db, commit_time="16:00-04:00")["rows"]}
    assert rows[402]["side"].startswith("before")
    assert rows[401]["side"].startswith("before")
    rows = {r["id"]: r for r in
            bwm.era_boundary_report(era_db, commit_time="16:00+00:00")["rows"]}
    assert rows[402]["side"].startswith("after")


def test_bare_commit_time_is_utc_and_the_report_says_so(era_db, capsys):
    assert bwm.commit_instant_utc("14:00").isoformat() == "2026-08-14T14:00:00+00:00"
    assert bwm.commit_instant_utc("14:32Z").isoformat() == "2026-08-14T14:32:00+00:00"
    assert bwm.commit_instant_utc("08:58:35-04:00").isoformat() == "2026-08-14T12:58:35+00:00"
    bwm.main(["--db", str(era_db), "--era-boundary-report",
              "--commit-time", "12:58:35+00:00"])
    out = capsys.readouterr().out
    assert "(UTC); rows are compared in UTC" in out
    assert "created_at (UTC)" in out


@pytest.mark.parametrize("good", ["12:58:35+00:00", "08:58-04:00", "14:32Z"])
def test_offset_commit_times_are_accepted(era_db, capsys, good):
    rc = bwm.main(["--db", str(era_db), "--era-boundary-report",
                   "--commit-time", good])
    assert rc == 0
    assert "unknown" not in capsys.readouterr().out


def test_help_asks_for_iso_strict(capsys):
    with pytest.raises(SystemExit):
        bwm.main(["--help"])
    out = capsys.readouterr().out
    assert "iso-strict" in out and "--apply-era-shift" in out


# --------------------------------------------------------------------------- #
# R4-FU A-04 (2026-10-02) — the boundary day is a UTC day, not a prefix
# --------------------------------------------------------------------------- #

@pytest.fixture
def offset_db(tmp_path) -> Path:
    """Rows whose stored offset puts their UTC day on the other side of
    the prefix: an import / hand edit shape (every in-tree writer stamps
    UTC)."""
    db = tmp_path / "offset_klog.sqlite"
    init_db(db)
    _insert_dated_row(db, 500, "2026-08-14T12:00:00+00:00", "kept", era=4)
    # prefix says the 15th; UTC instant is 2026-08-14T21:30 -> ON the day
    _insert_dated_row(db, 501, "2026-08-15T01:30:00+04:00", "kept", era=4)
    # prefix says the 14th; UTC instant is 2026-08-15T02:30 -> NOT on the day
    _insert_dated_row(db, 502, "2026-08-14T22:30:00-04:00", "neutral", era=4)
    # prefix says the 13th; UTC instant is 2026-08-14T03:00 -> ON the day
    _insert_dated_row(db, 503, "2026-08-13T23:00:00-04:00", "reverted", era=3)
    return db


def test_era_report_selects_the_day_on_the_utc_instant(offset_db):
    report = bwm.era_boundary_report(offset_db, commit_time="12:58:35+00:00")
    rows = {r["id"]: r for r in report["rows"]}
    assert sorted(rows) == [500, 501, 503]
    assert rows[501]["created_at_utc"] == "2026-08-14T21:30:00+00:00"
    assert rows[503]["created_at_utc"] == "2026-08-14T03:00:00+00:00"
    assert rows[501]["side"].startswith("after")
    assert rows[503]["side"].startswith("before")
    # the stored stamp is still carried verbatim beside the instant
    assert rows[501]["created_at"] == "2026-08-15T01:30:00+04:00"


def test_era_report_prints_the_utc_instant_and_the_stored_stamp(offset_db, capsys):
    rc = bwm.main(["--db", str(offset_db), "--era-boundary-report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 row(s) on 2026-08-14 (UTC)" in out
    assert "2026-08-14T21:30:00+00:00  (stored 2026-08-15T01:30:00+04:00)" in out
    assert "   502  " not in out


def test_apply_era_shift_relabels_the_utc_day_only(offset_db):
    result = bwm.apply_era_shift(offset_db)
    changed = {row_id: (before, after) for row_id, before, after in result["changed"]}
    # The classifier is untouched (it still reads the stored prefix), so
    # only the rows whose STORED stamp it labels differently move.
    assert 502 not in changed
    assert set(changed) <= {500, 501, 503}
    assert _eras(offset_db)[502] == 4


# --------------------------------------------------------------------------- #
# R4-FU A-12 (2026-10-02) — legacy era-4 AB-shaped rows with margin = 0
# --------------------------------------------------------------------------- #

def _insert_legacy_row(db: Path, row_id: int, margin, win_old, win_new,
                       sim_report) -> None:
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "INSERT INTO iterations (id, deck_id, deck_name, bracket, verdict, "
            "margin, win_rate_old, win_rate_new, sim_report, created_at) "
            "VALUES (?, 'd', 'D', 3, 'neutral', ?, ?, ?, ?, "
            "'2026-08-20T10:00:00+00:00')",
            (row_id, margin, win_old, win_new, json.dumps(sim_report)),
        )
        conn.commit()


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    db = tmp_path / "legacy_klog.sqlite"
    init_db(db)
    # The legacy shape: AB-shaped report, margin stored as 0, no win rates.
    _insert_legacy_row(db, 600, 0, None, None, {"wins_a": 0, "wins_b": 0, "games": 20})
    # Measured tie: margin 0 WITH win rates -> stays 0.
    _insert_legacy_row(db, 601, 0, 0.5, 0.5, {"wins_a": 10, "wins_b": 10, "games": 20})
    # Not margin 0 -> untouched even with NULL rates.
    _insert_legacy_row(db, 602, 3, None, None, {"wins_a": 2, "wins_b": 5, "games": 20})
    # Below the fence -> never touched.
    _insert_legacy_row(db, 300, 0, None, None, {"wins_a": 0, "wins_b": 0, "games": 20})
    return db


def test_dry_run_lists_the_legacy_zero_margin_rows_without_writing(legacy_db, capsys):
    summary = bwm.backfill(legacy_db, apply=False)
    assert summary["legacy_zero_margin_ids"] == [600]
    assert summary["changes"] == []
    assert _margin_of(legacy_db, 600) == 0
    bwm.print_report(summary)
    out = capsys.readouterr().out
    assert "legacy era-4 rows with margin = 0 and NO win rates" in out
    assert "ids 600" in out and "nothing to do" not in out


def test_apply_nulls_only_the_legacy_shape(legacy_db):
    summary = bwm.backfill(legacy_db, apply=True)
    assert summary["legacy_zero_margin_ids"] == [600]
    assert _margin_of(legacy_db, 600) is None
    assert _margin_of(legacy_db, 601) == 0
    assert _margin_of(legacy_db, 602) == 3
    assert _margin_of(legacy_db, 300) == 0
    # idempotent: a NULLed row no longer matches the shape
    assert bwm.backfill(legacy_db, apply=True)["legacy_zero_margin_ids"] == []
