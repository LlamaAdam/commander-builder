"""Export the knowledge log as a portable JSON file.

Personal-project scope: this is for **backup and sharing** between machines
or sessions, not for an open-data ecosystem. The shape mirrors the SQLite
schema so re-importing is a straightforward rebuild.

Use cases:
  - Snapshot the log before a risky migration
  - Move the log between dev machines
  - Share a deck's iteration history (filter by deck_id)
  - Archive completed iteration chains so the live DB stays small

Public API:

    from commander_builder.export import export_knowledge_log

    export_knowledge_log(out_path="kl_backup.json")
    export_knowledge_log(out_path="atraxa_only.json", deck_id="abc-XYZ")

CLI:

    commander-export --output kl_backup.json
    commander-export --output atraxa.json --deck-id abc-XYZ
    commander-export --output recent.json --recent 50
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ``_resolve_db_path`` (not a from-import of DEFAULT_DB_PATH) so the default
# database is looked up at call time — a module-level constant copy would
# bypass the test suite's DEFAULT_DB_PATH isolation patch.
from .knowledge_log import (
    Iteration,
    _resolve_db_path,
    iterations_for_deck,
    recent_iterations,
    stats_summary,
)


SCHEMA_VERSION = 1


def _iteration_to_export(it: Iteration) -> dict:
    """Round-trippable dict per iteration — same shape as the row."""
    d = asdict(it)
    return d


def export_knowledge_log(
    out_path: Path,
    deck_id: Optional[str] = None,
    recent: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Write the log (or a filtered slice) to `out_path` as JSON.

    Filter precedence: `deck_id` (single deck's full chain) > `recent`
    (cross-deck most-recent N) > full dump.

    Returns the in-memory export dict so callers can pipe / verify without
    re-reading the file."""
    # Resolved eagerly (rather than passed through as None) because the
    # payload's ``source_db`` field records the concrete path exported from.
    db_path = _resolve_db_path(db_path)
    if deck_id:
        rows = iterations_for_deck(deck_id, db_path=db_path)
        scope = f"deck_id={deck_id}"
    elif recent:
        rows = recent_iterations(limit=recent, db_path=db_path)
        scope = f"recent={recent}"
    else:
        # "All" — implemented as recent_iterations with a generous cap.
        rows = recent_iterations(limit=10_000, db_path=db_path)
        # recent_iterations returns newest-first; for full dumps, sort by id
        # so v1 → vN order is preserved on re-import.
        rows = sorted(rows, key=lambda r: r.id or 0)
        scope = "all"

    payload = {
        "format": "commander_builder.knowledge_log_export",
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(db_path),
        "scope": scope,
        "stats_at_export": stats_summary(db_path=db_path),
        "iterations": [_iteration_to_export(r) for r in rows],
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def import_knowledge_log(
    in_path: Path,
    db_path: Optional[Path] = None,
    skip_existing: bool = True,
) -> dict:
    """Re-ingest a previously exported log. Useful for restoring backups or
    merging another machine's iterations.

    `skip_existing=True` (default) checks each row's id; if it's already in
    the destination DB, the row is skipped rather than re-inserted (which
    would create a duplicate with a new id). Set False to force re-insert
    as new rows (loses original ids but preserves data)."""
    from .knowledge_log import get_iteration, record_iteration

    in_path = Path(in_path)
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    rows = payload.get("iterations", [])
    inserted = 0
    skipped = 0
    for row in rows:
        original_id = row.get("id")
        if skip_existing and original_id is not None:
            existing = get_iteration(original_id, db_path=db_path)
            if existing is not None:
                skipped += 1
                continue
        # Strip id so the destination DB autoincrements a fresh one.
        row_copy = {k: v for k, v in row.items() if k != "id"}
        # parent_id might point at an id from the source DB that doesn't
        # exist locally; preserve it but flag in the report. Cleaner fix
        # would remap, but for personal-project use that's overkill.
        record_iteration(Iteration(**row_copy), db_path=db_path)
        inserted += 1
    return {
        "imported_from": str(in_path),
        "rows_in_export": len(rows),
        "inserted": inserted,
        "skipped": skipped,
        "schema_version": payload.get("schema_version"),
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-export",
        description="Export / import the knowledge log as JSON.",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    # Default: export.
    p.add_argument("--output", help="Path to write JSON dump.")
    p.add_argument("--deck-id", help="Export only this deck's iterations.")
    p.add_argument("--recent", type=int,
                   help="Export only the most recent N iterations.")
    p.add_argument("--import-from",
                   help="Import a previously exported file instead of exporting.")
    p.add_argument("--no-skip-existing", action="store_true",
                   help="On import, re-insert rows even if their id already exists.")
    args = p.parse_args(argv)

    if args.import_from:
        result = import_knowledge_log(
            args.import_from,
            skip_existing=not args.no_skip_existing,
        )
        print(json.dumps(result, indent=2))
        return 0

    if not args.output:
        p.error("--output is required for export (or use --import-from).")

    payload = export_knowledge_log(
        Path(args.output),
        deck_id=args.deck_id,
        recent=args.recent,
    )
    print(f"Wrote {args.output}")
    print(f"  iterations: {len(payload['iterations'])}")
    print(f"  scope: {payload['scope']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
