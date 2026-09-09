"""One-off maintenance: backfill signed head-to-head margins for web-saved
knowledge-log rows written before the signed-margin fix (2026-08-13).

WHY: ``/api/save_iteration`` used to store the ``margin`` key straight out
of its ``sim_report`` payload. The web UI forwards the /api/propose_swap
response body there, whose ``margin`` is ``ComparisonReport.margin`` =
``abs(new_wins - old_wins)`` (the "decided by N games" display value) —
so every web-saved REGRESSION landed with a positive margin, reading as
"new deck ahead" in cross-run analyses, while the CLI writers stored the
signed ``new_wins - old_wins`` the knowledge_log schema documents. This
script recomputes ``margin = new_wins - old_wins`` from each affected
row's persisted ``sim_report`` JSON (the win counts themselves were never
corrupted — only the derived column was). Rows whose head-to-head
decisive count (``old_wins + new_wins``) is 0 get ``margin = NULL``, the
same no-fabricated-zero rule the win-rate columns and the fixed writer
follow.

HARD FENCE — rows with id < 314 are NEVER touched. Iteration 314 is the
seat-attribution fix boundary: rows before it were recorded while the sim
harness could mis-credit wins to the wrong seat, so their persisted
``sim_report`` win counts are themselves suspect. Recomputing margin from
those counts would launder pre-fix artifacts into a column that then
reads as clean, comparable data — worse than leaving the known-bad rows
visibly out of convention. The fence is a module constant, not a flag, on
purpose: there is no legitimate reason to lower it.

Row selection is shape-based: only rows whose ``sim_report`` is a JSON
object carrying ``old_wins``/``new_wins`` (the compare-shaped payload the
web writer and iteration_loop persist) are candidates. AB-shaped rows
(``wins_a``/``wins_b``, written by the auto-curate path, which always
stored signed margins) are skipped untouched. The recompute is idempotent
— already-correct rows show up as no-ops.

Dry-run by default; pass ``--apply`` to write. Prints a per-row
before/after table either way.

``--era-boundary-report`` — the 2026-08-14 era question (R2-D5)
---------------------------------------------------------------
A SECOND, entirely separate mode that shares this script only because
this is where "look before you touch the owner's only copy of the
history" already lives. It performs ZERO writes: no UPDATE, no INSERT,
no DELETE, no schema migration — it SELECTs and prints.

Why it exists (decision R2-D5, 2026-08-20; finding R2-P13):
``knowledge_log._SIGNIFICANCE_START`` puts the era-3/4 boundary at a bare
date, ``2026-08-14``, while the same function refuses to guess for the
other two ambiguous windows (the 2026-05-21/22 seat-fix session and the
2026-07-19 mixed-denominator window both resolve to NULL). But the
significance-verdict change was a COMMIT, not a midnight cutover, so any
row written on the morning of 2026-08-14 — before that commit landed —
carries an old margin-threshold verdict and is nonetheless stamped era 4,
which admits it straight into the FP-013 training floor.

Whether such rows exist is a fact about the owner's machine, not about
this repo. This mode lists every candidate row so the owner can answer it
on their own data and decide whether to move the boundary. Following the
D1 precedent: report first, owner applies (or doesn't) themselves.

Timezones (2026-09-03, R3 C-07): ``created_at`` is UTC (``datetime.now(
timezone.utc)``), so the boundary day is the UTC day and ``--commit-time``
is compared in UTC. Read the commit time with ``git log --date=iso-strict``
and pass it WITH its offset (``12:58:35+00:00``, ``08:58-04:00``); a bare
``HH:MM`` is taken as UTC and the report says so. The old lexical
HH:MM compare "sidestepped the timezone question" while the help text
asked for the LOCAL wall-clock time — every row shifted by the owner's
UTC offset.

``--apply-era-shift`` (2026-09-03, R3 C-05) — the ONE write this mode has
--------------------------------------------------------------------------
The advice this report used to print ("move ``_SIGNIFICANCE_START`` to
the next day, or NULL the day") could not be followed: the v3 backfill
touches only rows whose era is NULL, so moving the constant alone never
relabels an already-stamped era-4 row; and NULLing the day by hand is
re-stamped to era 4 by the very next ``init_db`` (any CLI, the web app)
unless the constant ALSO moved. Only both together work. So:

  step 1  ``--era-boundary-report --apply-era-shift`` writes
          ``measurement_era = <era if shifted>`` for every row on the
          boundary day (the whole UTC day — the granularity the library
          constant can express). Stored stamps are never rewritten by
          ``init_db``, so this holds across restarts.
  step 2  edit ``knowledge_log._SIGNIFICANCE_START`` to the shifted date,
          so anything that RE-DERIVES an era from a timestamp (an
          export/import round trip, a merge_soak fold) agrees with the
          stored stamps.

The report prints both steps verbatim; ``--apply`` (the margin backfill)
is still refused alongside the report flag.

Usage:
    python scripts/backfill_web_margins.py                # dry-run, default DB
    python scripts/backfill_web_margins.py --apply        # write changes
    python scripts/backfill_web_margins.py --db path.sqlite

    # report-only, never writes:
    python scripts/backfill_web_margins.py --era-boundary-report
    # ...and once `git log --date=iso-strict` says when the significance
    # commit landed (no line continuation here: this docstring is
    # argparse's description, and a trailing backslash would splice
    # the lines):
    python scripts/backfill_web_margins.py --era-boundary-report --commit-time 12:58:35+00:00
    # ...and, having decided the day is contaminated, relabel it:
    python scripts/backfill_web_margins.py --era-boundary-report --apply-era-shift
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Make the package importable when run as a loose script (same pattern as
# the other scripts/ maintenance utilities, e.g. margin_analysis.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from commander_builder.knowledge_log import (  # noqa: E402
    DEFAULT_DB_PATH,
    _SIGNIFICANCE_START,
    _connect,
    decisive_margin,
    measurement_era_for,
)

# Seat-attribution fix boundary. See the module docstring — rows below
# this id carry win counts recorded under the mis-attribution bug and
# must not be recomputed into apparent cleanliness.
MIN_ROW_ID = 314

# --- --era-boundary-report constants (R2-D5) -------------------------------

#: The one ambiguous day. Rows created on this date are stamped era 4 by
#: the bare-date rule, whether or not the significance commit had landed
#: when they were written.
ERA_BOUNDARY_DATE = _SIGNIFICANCE_START

#: What the boundary would move TO if the owner decides the day is
#: contaminated: era 4 starts the DAY AFTER, exactly the treatment the
#: other two ambiguous windows already get. Derived from the constant, so
#: a change to _SIGNIFICANCE_START can never leave this report reasoning
#: about a boundary the library no longer uses.
_SHIFTED_SIGNIFICANCE_START = (
    date.fromisoformat(ERA_BOUNDARY_DATE) + timedelta(days=1)
).isoformat()

#: ``--commit-time`` accepts HH:MM or HH:MM:SS, 24-hour, zero-padded,
#: optionally followed by a UTC offset (``Z``, ``+00:00``, ``-04:00``) as
#: ``git log --date=iso-strict`` prints it. Without an offset the time is
#: taken as UTC (see ``_side_of_landing``; R3 C-07).
_COMMIT_TIME_RE = re.compile(
    r"(?P<time>([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?)"
    r"(?P<offset>Z|[+-]([01]\d|2[0-3]):[0-5]\d)?"
)


def recompute_margin(sim_report: object) -> tuple[bool, Optional[int]]:
    """``(recognized, margin)`` for one row's parsed sim_report.

    ``recognized`` is False when the payload is not the compare-shaped
    dict this backfill covers (missing/non-dict report, AB-shaped
    ``wins_a``/``wins_b`` rows, non-integer counts) — such rows are
    skipped, never rewritten. When recognized, ``margin`` is the signed
    ``new_wins - old_wins``, or ``None`` when no game was decisive
    (``old_wins + new_wins == 0``) — NULL, not a fabricated 0.
    """
    if not isinstance(sim_report, dict):
        return False, None
    if "old_wins" not in sim_report or "new_wins" not in sim_report:
        return False, None
    try:
        old_w = int(sim_report.get("old_wins") or 0)
        new_w = int(sim_report.get("new_wins") or 0)
    except (TypeError, ValueError):
        return False, None
    # The shared convention helper (2026-09-03, R3 C-14): NULL when no
    # game was decisive, else the signed delta — the same call every
    # live writer makes, so this backfill cannot drift from them.
    return True, decisive_margin(old_w, new_w)


def backfill(db_path: Path, apply: bool = False) -> dict:
    """Scan rows with id >= MIN_ROW_ID, recompute margins, optionally write.

    Returns a summary dict::

        {"scanned": int, "skipped": int, "unchanged": int,
         "changes": [{"id", "old_margin", "new_margin",
                      "old_wins", "new_wins"}, ...],
         "applied": bool}
    """
    scanned = 0
    skipped = 0
    unchanged = 0
    changes: list[dict] = []

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, margin, sim_report FROM iterations "
            "WHERE id >= ? ORDER BY id ASC",
            (MIN_ROW_ID,),
        ).fetchall()
        for row in rows:
            scanned += 1
            raw = row["sim_report"]
            try:
                report = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                report = None
            recognized, new_margin = recompute_margin(report)
            if not recognized:
                skipped += 1
                continue
            if new_margin == row["margin"]:
                unchanged += 1
                continue
            changes.append({
                "id": row["id"],
                "old_margin": row["margin"],
                "new_margin": new_margin,
                "old_wins": int(report.get("old_wins") or 0),
                "new_wins": int(report.get("new_wins") or 0),
            })
        if apply:
            for change in changes:
                conn.execute(
                    "UPDATE iterations SET margin = ? WHERE id = ?",
                    (change["new_margin"], change["id"]),
                )

    return {
        "scanned": scanned,
        "skipped": skipped,
        "unchanged": unchanged,
        "changes": changes,
        "applied": apply,
    }


def _fmt(value: Optional[int]) -> str:
    return "NULL" if value is None else f"{value:+d}"


def print_report(summary: dict) -> None:
    """Human-readable per-row before/after table + totals."""
    mode = "APPLIED" if summary["applied"] else "DRY-RUN (pass --apply to write)"
    print(f"backfill_web_margins — {mode}")
    print(f"  fence: only rows with id >= {MIN_ROW_ID} "
          f"(seat-attribution fix boundary) are considered")
    print(f"  scanned: {summary['scanned']}   "
          f"skipped (not compare-shaped): {summary['skipped']}   "
          f"already correct: {summary['unchanged']}   "
          f"to change: {len(summary['changes'])}")
    if not summary["changes"]:
        print("  nothing to do.")
        return
    print(f"  {'id':>6}  {'old_wins':>8}  {'new_wins':>8}  "
          f"{'margin before':>13}  {'margin after':>12}")
    for c in summary["changes"]:
        print(f"  {c['id']:>6}  {c['old_wins']:>8}  {c['new_wins']:>8}  "
              f"{_fmt(c['old_margin']):>13}  {_fmt(c['new_margin']):>12}")


# ---------------------------------------------------------------------------
# --era-boundary-report (R2-D5, 2026-08-20) — REPORT ONLY, ZERO WRITES
# ---------------------------------------------------------------------------

def commit_instant_utc(commit_time: str) -> datetime:
    """``--commit-time`` on the boundary date as an aware UTC datetime.

    ``12:58:35+00:00`` / ``08:58-04:00`` / ``14:32Z`` carry their offset;
    a bare ``14:32`` is UTC (R3 C-07, 2026-09-03). Raises ``ValueError``
    on anything else — ``main`` refuses before getting here.
    """
    m = _COMMIT_TIME_RE.fullmatch(commit_time)
    if not m:
        raise ValueError(f"malformed --commit-time {commit_time!r}")
    offset = m.group("offset") or "+00:00"
    if offset == "Z":
        offset = "+00:00"
    stamp = datetime.fromisoformat(f"{ERA_BOUNDARY_DATE}T{m.group('time')}{offset}")
    return stamp.astimezone(timezone.utc)


def _row_instant_utc(created_at: str) -> Optional[datetime]:
    """A row's ``created_at`` as an aware UTC datetime, or None when it
    is not a parseable ISO timestamp. A naive stamp is taken as UTC —
    every in-tree writer stamps ``datetime.now(timezone.utc)``."""
    try:
        stamp = datetime.fromisoformat(created_at.strip())
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _side_of_landing(created_at: str, commit_time: Optional[str]) -> str:
    """Which side of the significance commit a row's timestamp falls on.

    ``commit_time`` is the owner's answer to "when did the significance
    commit actually land on 2026-08-14?" — read off ``git log
    --date=iso-strict``, offset included. Without it the honest answer
    is ``unknown``: nothing in the database records when the code
    changed, and guessing is precisely the failure mode the
    NULL-not-guess rule exists to prevent.

    Both sides are converted to UTC before comparing (2026-09-03, R3
    C-07). The previous lexical ``HH:MM`` compare put a UTC row time
    next to whatever zone the owner read off ``git log`` — for any
    non-UTC owner every row was shifted by the offset.
    """
    if not commit_time:
        return "unknown"
    row = _row_instant_utc(created_at)
    if row is None:
        return "unknown"
    return ("before (would move to era 3)"
            if row < commit_instant_utc(commit_time)
            else "after (stays era 4)")


def era_boundary_report(
    db_path: Path, commit_time: Optional[str] = None,
) -> dict:
    """List every row created on ``ERA_BOUNDARY_DATE``. Reads only.

    Returns::

        {"boundary_date", "shifted_start", "commit_time",
         "rows": [{"id", "created_at", "verdict", "era",
                   "era_if_shifted", "side"}, ...]}

    ``era`` is the row's STORED stamp (what the FP-013 floor actually
    reads today). ``era_if_shifted`` is what ``measurement_era_for``
    returns for the same row if the era-3/4 boundary moved to
    ``_SHIFTED_SIGNIFICANCE_START`` — computed by calling that ONE
    function with an override, never by a second copy of its rules.

    Every row on the day is listed, including any whose stored stamp
    already disagrees with the date rule (a hand-edited or imported
    row). Filtering those out would hide exactly the surprises the owner
    is running this to find.
    """
    rows: list[dict] = []
    with _connect(db_path) as conn:
        # SELECT only. This function performs no writes of any kind.
        found = conn.execute(
            "SELECT id, created_at, verdict, measurement_era FROM iterations "
            "WHERE created_at LIKE ? ORDER BY created_at ASC, id ASC",
            (f"{ERA_BOUNDARY_DATE}%",),
        ).fetchall()
    for row in found:
        created_at = row["created_at"] or ""
        rows.append({
            "id": row["id"],
            "created_at": created_at,
            "verdict": row["verdict"],
            "era": row["measurement_era"],
            "era_if_shifted": measurement_era_for(
                created_at, row["id"],
                significance_start=_SHIFTED_SIGNIFICANCE_START,
            ),
            "side": _side_of_landing(created_at, commit_time),
        })
    return {
        "boundary_date": ERA_BOUNDARY_DATE,
        "shifted_start": _SHIFTED_SIGNIFICANCE_START,
        "commit_time": commit_time,
        "rows": rows,
    }


def apply_era_shift(db_path: Path) -> dict:
    """Write ``measurement_era = era_if_shifted`` for every row on the
    boundary day (R3 C-05, 2026-09-03). Returns ``{"changed": [(id,
    before, after), ...], "unchanged": int}``. Idempotent: a row already
    carrying the shifted era is a no-op.

    Whole UTC day, deliberately: ``_SIGNIFICANCE_START`` is a bare date,
    so the day is the finest boundary step 2 (moving the constant) can
    express; relabeling only the before-commit rows would leave the
    stored stamps and the constant disagreeing on the after-commit ones.
    ``era_if_shifted`` comes from ``measurement_era_for`` with the
    override — the one definition of the rules — never a literal 3.
    """
    report = era_boundary_report(db_path)
    changed: list[tuple[int, Optional[int], Optional[int]]] = []
    unchanged = 0
    with _connect(db_path) as conn:
        for row in report["rows"]:
            if row["era"] == row["era_if_shifted"]:
                unchanged += 1
                continue
            conn.execute(
                "UPDATE iterations SET measurement_era = ? WHERE id = ?",
                (row["era_if_shifted"], row["id"]),
            )
            changed.append((row["id"], row["era"], row["era_if_shifted"]))
    return {"changed": changed, "unchanged": unchanged}


def print_era_shift(result: dict, shifted_start: str) -> None:
    print(f"\n  --apply-era-shift APPLIED: {len(result['changed'])} row(s) "
          f"relabeled, {result['unchanged']} already carried the shifted "
          f"era.")
    for row_id, before, after in result["changed"]:
        print(f"    id {row_id}: era {before} -> {after}")
    print(f"\n  STEP 2 (not done by this script — a source edit): set "
          f"knowledge_log._SIGNIFICANCE_START = \"{shifted_start}\" so "
          f"anything that re-derives an era from a timestamp (export/"
          f"import, merge_soak) agrees with the stamps just written.")


def print_era_boundary_report(report: dict) -> None:
    """Human-readable listing. Labeled REPORT ONLY so nobody reads it as
    a dry-run of a write this mode does not have (its one write is the
    explicit ``--apply-era-shift``, printed separately)."""
    print("backfill_web_margins — ERA BOUNDARY REPORT ONLY (no writes "
          "unless --apply-era-shift is given; --apply is refused here)")
    print(f"  question (decision R2-D5): were any knowledge_log rows "
          f"written on {report['boundary_date']} (UTC day) BEFORE the "
          f"significance-verdict commit landed?")
    print(f"  today those rows are stamped era 4 by a bare date cut and "
          f"count toward the FP-013 training floor.")
    print(f"  if the boundary moved to {report['shifted_start']}, they "
          f"would be era 3 (recoverable by re-scoring, not lost).")
    rows = report["rows"]
    if not rows:
        print(f"\n  NO rows created on {report['boundary_date']} in this "
              f"database.")
        print("  -> the window is empty here; leave "
              "knowledge_log._SIGNIFICANCE_START as it is.")
        return
    if not report["commit_time"]:
        print("\n  NOTE: --commit-time was not given, so the 'side' column "
              "is 'unknown'. Nothing in the database records when the code "
              "changed. Read the commit's time off `git log "
              "--date=iso-strict` and re-run with --commit-time "
              "HH:MM[:SS][+hh:mm] (offset as printed; a bare time is UTC) "
              "to split the rows.")
    else:
        print(f"\n  commit time {report['commit_time']} = "
              f"{commit_instant_utc(report['commit_time']).isoformat()} "
              f"(UTC); rows are compared in UTC.")
    print(f"\n  {len(rows)} row(s) on {report['boundary_date']} (UTC):\n")
    print(f"  {'id':>6}  {'created_at (UTC)':<32}  {'verdict':<13}  "
          f"{'era':>4}  {'if shifted':>10}  side")
    for r in rows:
        era = "NULL" if r["era"] is None else str(r["era"])
        shifted = ("NULL" if r["era_if_shifted"] is None
                   else str(r["era_if_shifted"]))
        print(f"  {r['id']:>6}  {r['created_at']:<32}  "
              f"{str(r['verdict']):<13}  {era:>4}  {shifted:>10}  "
              f"{r['side']}")
    print("\n  Decide on your own data: if every row above was written "
          "AFTER the commit, leave the constant alone and note the "
          "window is clean. If any predate it, BOTH of these are needed "
          "(either alone does nothing that lasts):")
    print("    step 1: re-run this report with --apply-era-shift to "
          "relabel the day's stored stamps to the 'if shifted' column;")
    print(f"    step 2: edit knowledge_log._SIGNIFICANCE_START to "
          f"\"{report['shifted_start']}\" so re-derived eras agree.")
    print("  Why both: init_db's backfill only fills NULL eras, so moving "
          "the constant never touches a stamped row; and a row NULLed by "
          "hand is re-stamped era 4 on the next init_db unless the "
          "constant moved too.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=None,
                    help=f"knowledge_log SQLite path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--apply", action="store_true",
                    help="write the recomputed margins (default: dry-run)")
    ap.add_argument("--era-boundary-report", action="store_true",
                    help=f"REPORT ONLY (never writes, ignores --apply): "
                         f"list every knowledge_log row created on "
                         f"{ERA_BOUNDARY_DATE} with its id, created_at, "
                         f"verdict, stored measurement era and the era it "
                         f"would carry if the era-3/4 boundary moved to "
                         f"{_SHIFTED_SIGNIFICANCE_START}. Decision R2-D5: "
                         f"the owner reviews this on their own machine "
                         f"and decides whether the boundary needs "
                         f"shifting.")
    ap.add_argument("--commit-time", default=None, metavar="HH:MM[:SS][+hh:mm]",
                    help=f"Only with --era-boundary-report. The time the "
                         f"significance-verdict commit landed on "
                         f"{ERA_BOUNDARY_DATE}, read off `git log "
                         f"--date=iso-strict` WITH its offset (e.g. "
                         f"12:58:35+00:00 or 08:58-04:00); a bare HH:MM is "
                         f"taken as UTC. Rows are compared in UTC, the zone "
                         f"created_at is written in. Splits the listed rows "
                         f"into before / after; without it the 'side' "
                         f"column reads 'unknown', because nothing in the "
                         f"database records when the code changed.")
    ap.add_argument("--apply-era-shift", action="store_true",
                    help=f"Only with --era-boundary-report. Relabel every "
                         f"row on {ERA_BOUNDARY_DATE} (UTC) to the era it "
                         f"would carry under a boundary of "
                         f"{_SHIFTED_SIGNIFICANCE_START} (step 1 of 2; the "
                         f"report prints step 2, a source edit). Default: "
                         f"report only.")
    args = ap.parse_args(argv)

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"ERROR: knowledge log not found: {db_path}", file=sys.stderr)
        return 2

    if args.commit_time and not _COMMIT_TIME_RE.fullmatch(args.commit_time):
        # No silent failures: the side column is a lexical string compare
        # against the ISO time field, so a malformed value would quietly
        # sort every row onto one side rather than erroring.
        print(f"ERROR: --commit-time must be HH:MM or HH:MM:SS (24-hour), "
              f"got {args.commit_time!r}", file=sys.stderr)
        return 2

    if args.era_boundary_report:
        # Report mode short-circuits BEFORE the backfill runs: the two
        # modes answer different questions, and letting --apply ride
        # along with a report flag is exactly the accident this
        # owner-reviews-first decision exists to prevent.
        if args.apply:
            print("ERROR: --era-boundary-report is report-only; the margin "
                  "backfill's --apply does not ride along. Drop --apply "
                  "(the era relabel is --apply-era-shift).", file=sys.stderr)
            return 2
        report = era_boundary_report(db_path, commit_time=args.commit_time)
        print_era_boundary_report(report)
        if args.apply_era_shift:
            if not report["rows"]:
                print("\n  --apply-era-shift: nothing to relabel.")
                return 0
            print_era_shift(apply_era_shift(db_path), report["shifted_start"])
        return 0
    if args.commit_time or args.apply_era_shift:
        print("ERROR: --commit-time / --apply-era-shift only applies to "
              "--era-boundary-report.", file=sys.stderr)
        return 2

    summary = backfill(db_path, apply=args.apply)
    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
