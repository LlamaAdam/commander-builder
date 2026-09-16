"""One-off maintenance: re-key knowledge-log rows whose ``deck_id`` was a
per-version filename stem onto the stable per-deck identity
(R3 C-08, 2026-09-03).

WHY: ``iteration_loop.resolve_deck_id`` read only ``Moxfield=<publicId>``
and otherwise fell back to the RAW filename stem — and the two unattended
writers (``_proposer_sim._log_auto_curate_iteration``, ``improve.
_log_bandit_pull``) passed the NEW deck's stem, which gains `` v2``,
`` v3`` … on every accepted round. So every hand-built deck and every
deck adopted through the Archidekt lane (``Archidekt=<id>`` /
``Source=archidekt``, no ``Moxfield=`` line) got a fresh ``deck_id`` per
iteration: ``commander-history``, ``/api/verdict_breakdown``, the
trajectory, the pricing series, the iteration graph and the
judge-agreement joins all saw one-row "decks". ``deck_identity`` now
keys new rows stably; this script brings the existing rows onto the same
key so the per-deck surfaces see one deck again.

WHAT IT CHANGES — ``deck_id`` only, and only on rows whose current id is
FILENAME-SHAPED (a ``.dck`` extension, a ``[B<n>]`` suffix, or a `` v<N>``
version): an explicit id (a Moxfield publicId, a test id, an already
namespaced ``archidekt:<id>``) is never second-guessed. The new id is
what ``deck_identity.resolve_deck_id`` would produce today for the row's
own ``deck_snapshot``: the snapshot's ``Moxfield=`` publicId, else its
``Archidekt=`` id under the ``archidekt:`` namespace, else the
version-stripped stem of the current id. No measurement column is read or
written — this is a KEY repair, not a relabel — and ``parent_id`` is
deliberately left alone: the auto-curate writer's missing parents could
only be re-threaded by guessing lineage from timestamps, and a guessed
parent is worse than a NULL one (the chain is correct for new rows).

Dry-run by default; pass ``--apply`` to write. Prints a per-row
before/after table either way, following ``backfill_web_margins.py``.
Idempotent — already-stable rows show up as no-ops.

Usage:
    python scripts/backfill_deck_ids.py                # dry-run, default DB
    python scripts/backfill_deck_ids.py --apply        # write changes
    python scripts/backfill_deck_ids.py --db path.sqlite
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# Make the package importable when run as a loose script (same pattern as
# the other scripts/ maintenance utilities, e.g. backfill_web_margins.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from commander_builder.deck_identity import (  # noqa: E402
    is_filename_shaped_deck_id,
    stable_deck_id_for_row,
)
from commander_builder.knowledge_log import (  # noqa: E402
    DEFAULT_DB_PATH,
    _connect,
)


def backfill(db_path: Path, apply: bool = False) -> dict:
    """Scan every row, compute the stable id, optionally write.

    Returns a summary dict::

        {"scanned": int, "skipped": int, "unchanged": int,
         "changes": [{"id", "old_deck_id", "new_deck_id"}, ...],
         "applied": bool}

    ``skipped`` counts rows whose id is not filename-shaped (never
    candidates); ``unchanged`` counts filename-shaped ids that already
    ARE the stable id.
    """
    scanned = 0
    skipped = 0
    unchanged = 0
    changes: list[dict] = []

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, deck_id, deck_snapshot FROM iterations ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            scanned += 1
            current = row["deck_id"] or ""
            if not is_filename_shaped_deck_id(current):
                skipped += 1
                continue
            new_id = stable_deck_id_for_row(current, row["deck_snapshot"])
            if new_id is None:
                unchanged += 1
                continue
            changes.append({
                "id": row["id"],
                "old_deck_id": current,
                "new_deck_id": new_id,
            })
        if apply:
            for change in changes:
                conn.execute(
                    "UPDATE iterations SET deck_id = ? WHERE id = ?",
                    (change["new_deck_id"], change["id"]),
                )

    return {
        "scanned": scanned,
        "skipped": skipped,
        "unchanged": unchanged,
        "changes": changes,
        "applied": apply,
    }


def print_report(summary: dict) -> None:
    """Human-readable per-row before/after table + totals."""
    mode = "APPLIED" if summary["applied"] else "DRY-RUN (pass --apply to write)"
    print(f"backfill_deck_ids — {mode}")
    print("  scope: deck_id only, on rows whose deck_id is a filename / "
          "stem; explicit ids are never touched, parent_id is never touched")
    print(f"  scanned: {summary['scanned']}   "
          f"skipped (explicit id): {summary['skipped']}   "
          f"already stable: {summary['unchanged']}   "
          f"to change: {len(summary['changes'])}")
    if not summary["changes"]:
        print("  nothing to do.")
        return
    width = max(len(c["old_deck_id"]) for c in summary["changes"])
    print(f"  {'id':>6}  {'deck_id before':<{width}}  deck_id after")
    for c in summary["changes"]:
        print(f"  {c['id']:>6}  {c['old_deck_id']:<{width}}  {c['new_deck_id']}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=None,
                    help=f"knowledge_log SQLite path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--apply", action="store_true",
                    help="write the stable deck_ids (default: dry-run)")
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
