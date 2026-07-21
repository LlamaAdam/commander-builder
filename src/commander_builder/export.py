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
    all_iterations,
    canonical_content_hash,
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
        # "All" means ALL. This used to be recent_iterations(limit=10_000),
        # which silently truncated any log past 10k rows while the export
        # still reported success — a backup that quietly loses the oldest
        # history is worse than no backup. git history shows the cap was
        # never protecting anything (it was just a lazy way to spell
        # "everything" via the recent-N query), so it's simply removed.
        # all_iterations() returns id-ASC, so v1 → vN order is preserved
        # on re-import without an extra sort.
        rows = all_iterations(db_path=db_path)
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


# Columns that never participate in content identity:
#   id        — autoincrement, machine-local. Two machines both start at 1,
#               so ids from different DBs collide on UNRELATED rows.
#   parent_id — a reference to one of those machine-local ids; meaningful
#               only within the DB it was written in. It is remapped (not
#               compared) on import.
_LOCAL_ONLY_COLUMNS = frozenset({"id", "parent_id"})


def _identity_key(row: dict) -> tuple:
    """Natural key for "is this the same iteration?" across machines.

    (deck_id, created_at, deck_name): created_at is a microsecond-precision
    ISO timestamp stamped once at record time and carried verbatim through
    export/import, so two independently-created iterations essentially never
    share a key, while the SAME iteration exported at different times (or
    after a later verdict/milestone PATCH) always does. deck_id + deck_name
    guard the astronomically-unlikely timestamp tie and make the key
    self-describing in debug output.
    """
    return (row.get("deck_id"), row.get("created_at"), row.get("deck_name"))


def _content_hash(row: dict) -> str:
    """Canonical hash of a row's semantic columns (everything except the
    machine-local id/parent_id). Used to distinguish "byte-for-byte the
    same iteration" from "same iteration, but verdict/sim/milestone fields
    have since diverged".

    The canonicalization itself (sort_keys so nested audit_manifest /
    sim_report dicts can't break equality on key order) lives in
    ``knowledge_log.canonical_content_hash`` — promoted there 2026-07-19
    so ``scripts/merge_soak.py``'s fold dedupe shares the identical hash
    instead of copy-pasting it. This wrapper just binds export/import's
    identity choice: everything semantic, minus the machine-local ids."""
    return canonical_content_hash(row, _LOCAL_ONLY_COLUMNS)


def import_knowledge_log(
    in_path: Path,
    db_path: Optional[Path] = None,
    skip_existing: bool = True,
) -> dict:
    """Re-ingest a previously exported log. Useful for restoring backups or
    merging another machine's iterations.

    Dedupe is by CONTENT IDENTITY, never by autoincrement id. The old
    implementation skipped any imported row whose ``id`` already existed
    locally — but when merging another machine's log both DBs start ids
    at 1, so nearly every imported row collided with an UNRELATED local
    row and was silently dropped while the summary claimed success.

    With ``skip_existing=True`` (default), each imported row is matched
    against local rows by natural key (deck_id, created_at, deck_name):

      * identical content (canonical hash over all semantic columns)
        → skipped, counted in ``skipped_identical``;
      * same natural key but differing mutable fields (verdict,
        sim_report, milestone — the PATCH-endpoint fields) → treated as
        the same iteration exported at a different time; skipped, local
        row wins, counted in ``skipped_existing_variant``. Tradeoff: we
        could merge "newer non-null verdict" into the local row, but
        picking a winner needs a reliable per-field timestamp we don't
        store — silently overwriting local verdicts on import would be
        its own data-loss bug, so we keep the conservative skip;
      * no local match → INSERTED with a FRESH sqlite-assigned id
        (never the source id), counted in ``inserted`` and, when the
        assigned id differs from the source id, in ``id_remapped``.

    ``parent_id`` is remapped through the source-id → local-id map built
    during the import (rows are processed in source-id order, so parents
    resolve before children). A parent that isn't in the export and can't
    be matched locally becomes NULL rather than a dangling/foreign id —
    counted in ``unresolved_parents``.

    Set ``skip_existing=False`` to force-insert every row as new (still
    with fresh ids and remapped parents)."""
    from .knowledge_log import all_iterations as _all_local, record_iteration

    in_path = Path(in_path)
    # Resolve once so the pre-scan and the per-row inserts hit the SAME DB
    # even if DEFAULT_DB_PATH is monkeypatched mid-call (test isolation).
    db_path = _resolve_db_path(db_path)
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    rows = payload.get("iterations", [])

    # Pre-scan the destination once and index it by natural key. Personal-
    # project scale (thousands of rows, not millions) makes an in-memory
    # index far simpler than per-row SQL lookups, and it also lets rows
    # inserted BY THIS import participate in dedupe (a file containing the
    # same row twice still dedupes to one).
    by_key: dict[tuple, list[tuple[int, str]]] = {}
    for it in _all_local(db_path=db_path):
        local = asdict(it)
        by_key.setdefault(_identity_key(local), []).append(
            (it.id, _content_hash(local))
        )

    # Process in source-id order so a chain's parent is imported (and its
    # new local id recorded) before any child that references it.
    rows = sorted(rows, key=lambda r: (r.get("id") is None, r.get("id") or 0))

    id_map: dict[int, int] = {}  # source id -> local id (inserted OR matched)
    inserted = 0
    id_remapped = 0
    skipped_identical = 0
    skipped_existing_variant = 0
    unresolved_parents = 0

    for row in rows:
        source_id = row.get("id")
        key = _identity_key(row)
        row_hash = _content_hash(row)
        matches = by_key.get(key, [])

        if skip_existing and matches:
            exact = next((lid for lid, lhash in matches if lhash == row_hash), None)
            if exact is not None:
                skipped_identical += 1
                matched_local_id = exact
            else:
                # Same iteration identity, but mutable fields drifted
                # (e.g. local verdict was PATCHed after the export was
                # taken). Local wins — see docstring for the tradeoff.
                skipped_existing_variant += 1
                matched_local_id = matches[0][0]
            # Even skipped rows enter the id map: a child in this export
            # may name this row as its parent, and the local match is the
            # correct target for that edge.
            if source_id is not None:
                id_map[source_id] = matched_local_id
            continue

        # Insert path: strip the machine-local columns; sqlite assigns a
        # fresh id. NEVER insert with the source id — on a merge it very
        # likely belongs to an unrelated local row.
        row_copy = {k: v for k, v in row.items() if k not in _LOCAL_ONLY_COLUMNS}
        source_parent = row.get("parent_id")
        if source_parent is not None:
            if source_parent in id_map:
                row_copy["parent_id"] = id_map[source_parent]
            else:
                # Parent isn't in this export and didn't match anything
                # local. Carrying the foreign id over would point the FK
                # at whatever unrelated local row happens to own that
                # number — NULL (chain root) is the honest fallback.
                row_copy["parent_id"] = None
                unresolved_parents += 1
        new_id = record_iteration(Iteration(**row_copy), db_path=db_path)
        inserted += 1
        if source_id is not None:
            id_map[source_id] = new_id
            if new_id != source_id:
                id_remapped += 1
        # Register the fresh row so a duplicate later in this same file
        # dedupes against it.
        by_key.setdefault(key, []).append((new_id, row_hash))

    return {
        "imported_from": str(in_path),
        "rows_in_export": len(rows),
        "inserted": inserted,
        "id_remapped": id_remapped,
        "skipped_identical": skipped_identical,
        "skipped_existing_variant": skipped_existing_variant,
        # Legacy aggregate kept so older callers/scripts reading "skipped"
        # keep working; it is now the sum of the two skip reasons.
        "skipped": skipped_identical + skipped_existing_variant,
        "unresolved_parents": unresolved_parents,
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
                   help="On import, insert every row as a new iteration even if "
                        "identical content already exists locally.")
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
