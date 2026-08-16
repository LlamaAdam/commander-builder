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

Usage:
    python scripts/backfill_web_margins.py                # dry-run, default DB
    python scripts/backfill_web_margins.py --apply        # write changes
    python scripts/backfill_web_margins.py --db path.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Make the package importable when run as a loose script (same pattern as
# the other scripts/ maintenance utilities, e.g. margin_analysis.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from commander_builder.knowledge_log import (  # noqa: E402
    DEFAULT_DB_PATH,
    _connect,
)

# Seat-attribution fix boundary. See the module docstring — rows below
# this id carry win counts recorded under the mis-attribution bug and
# must not be recomputed into apparent cleanliness.
MIN_ROW_ID = 314


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
    decisive = old_w + new_w
    if decisive <= 0:
        return True, None
    return True, new_w - old_w


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


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=None,
                    help=f"knowledge_log SQLite path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--apply", action="store_true",
                    help="write the recomputed margins (default: dry-run)")
    args = ap.parse_args(argv)

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"ERROR: knowledge log not found: {db_path}", file=sys.stderr)
        return 2

    summary = backfill(db_path, apply=args.apply)
    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
