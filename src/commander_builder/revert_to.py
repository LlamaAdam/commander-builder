"""Roll back a deck to a previous iteration's state.

When `analyst.analyze` returns `"reverted"`, the iteration loop logs the
verdict but doesn't unwind the change — Moxfield is still showing the v2
deck. `revert_to.py` reads the prior iteration's `deck_snapshot` blob from
`knowledge_log` (which is exactly the v1 .dck text we preserved on insert)
and:

  1. Writes the snapshot back to the `vendor/forge/.../commander/` directory
     so local tooling sees the v1 state.
  2. Generates a clipboard-ready Moxfield textarea blob via `moxfield_push`.

The user still has to paste the blob into Moxfield (manual push). When
`moxfield_push._api_push` is wired (auth-token availability), this module
becomes fully automated.

CLI:

    commander-revert --to-iteration 7
    # or:
    commander-revert --to-deck "[USER] My Deck [B3].dck" --version 1
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .forge_runner import VENDOR_FORGE
# ``db_path=None`` defaults below defer to knowledge_log's call-time
# resolver — a ``= DEFAULT_DB_PATH`` def-time default would freeze the
# production path and bypass the test suite's isolation patch.
from .knowledge_log import (
    Iteration,
    get_iteration,
    iterations_for_deck,
    record_iteration,
)
from .moxfield_push import dck_to_textarea, prepare_push

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


@dataclass
class RevertResult:
    iteration_id: int                # The iteration we reverted TO
    restored_path: Path              # On-disk .dck overwritten with the snapshot
    push_blob: str                   # Moxfield-textarea blob for manual push
    revert_iteration_id: Optional[int] = None  # New row recording this revert
    # Copy of the pre-revert on-disk file, or None when no backup was needed
    # (file absent, or already identical to the restored snapshot).
    backup_path: Optional[Path] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["restored_path"] = str(self.restored_path)
        d["backup_path"] = str(self.backup_path) if self.backup_path else None
        d.pop("push_blob")  # Not interesting in the structured output.
        return d


def _backup_destination(out_path: Path) -> Path:
    """Pick a free sibling path for the pre-revert backup.

    Shape: ``<stem>.pre-revert-<YYYYMMDD_HHMMSS>.dck.bak`` (timestamp format
    matches routes_sim's staged-file convention). The ``.bak`` FINAL suffix is
    load-bearing: every deck-listing consumer (status._count_decks,
    moxfield_import._existing_moxfield_ids, the web app's deck routes) globs
    ``*.dck``, so a backup ending in ``.bak`` can never pollute deck listings
    or get picked up as a playable deck — even though the name still carries
    ``.dck`` internally so a human can tell what it is and rename it back.

    The timestamp granularity is 1 second, so two reverts in the same second
    would collide and the second backup would overwrite the first — exactly
    the data loss this module is trying to prevent. Uniquify with a small
    counter loop (same idea as moxfield_import._uniquify, but local: pulling
    that helper across modules just for this would couple the two for no
    gain)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = out_path.with_name(f"{out_path.stem}.pre-revert-{ts}.dck.bak")
    n = 2
    while candidate.exists():
        candidate = out_path.with_name(
            f"{out_path.stem}.pre-revert-{ts}-{n}.dck.bak"
        )
        n += 1
    return candidate


def _backup_current_file(out_path: Path, snapshot: str) -> Optional[Path]:
    """Copy the live deck file aside before it gets overwritten.

    The knowledge log only holds states that went through record_iteration.
    If the on-disk file was hand-edited (or re-pulled outside the loop), its
    content exists NOWHERE else — overwriting it in revert_to_iteration would
    destroy it unrecoverably. Hence: unconditional copy-aside first.

    Returns the backup path, or None when nothing needed saving:
      - file doesn't exist (nothing to lose), or
      - current content already equals the snapshot being restored (no
        information would be lost by overwriting).

    The "already equals" check compares via read_text, not raw bytes, because
    the restore below goes through write_text — which maps "\\n" to the
    platform line separator on the way out while read_text's universal-newline
    mode maps it back. Comparing decoded text is therefore exactly the
    "would the restore change anything observable?" question. If the file
    isn't valid UTF-8 at all, we can't answer that question — treat it as
    different and back it up (erring toward keeping data)."""
    if not out_path.exists():
        return None
    try:
        if out_path.read_text(encoding="utf-8") == snapshot:
            return None
    except (UnicodeDecodeError, OSError):
        pass  # Unreadable-as-text ≠ safe to discard: fall through and back up.
    backup = _backup_destination(out_path)
    # copy2 preserves the original bytes AND mtime — the mtime is a forensic
    # clue about when the lost-to-the-log edit was actually made.
    shutil.copy2(out_path, backup)
    return backup


def revert_to_iteration(
    iteration_id: int,
    deck_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    record_revert: bool = True,
) -> RevertResult:
    """Restore the .dck file to the snapshot stored at the given iteration.

    `deck_path` defaults to the iteration's `deck_name` resolved against
    `DECK_DIR`. Pass an explicit path when reverting to a different filename
    (e.g. user renamed the deck since)."""
    target = get_iteration(iteration_id, db_path=db_path)
    if target is None:
        raise ValueError(f"iteration {iteration_id} not found")
    if not target.deck_snapshot:
        raise ValueError(
            f"iteration {iteration_id} has no deck_snapshot — cannot reconstruct .dck"
        )

    out_path = deck_path or (DECK_DIR / target.deck_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Save the current file BEFORE overwriting — see _backup_current_file for
    # why (un-logged on-disk state is otherwise gone forever).
    backup_path = _backup_current_file(out_path, target.deck_snapshot)
    out_path.write_text(target.deck_snapshot, encoding="utf-8")

    blob = dck_to_textarea(out_path)

    revert_id: Optional[int] = None
    if record_revert:
        # Log the revert as its own iteration so the chain stays auditable.
        # parent_id points back at the iteration we reverted FROM, not TO,
        # because that's the most recent state in the history.
        recent = iterations_for_deck(target.deck_id, db_path=db_path)
        latest = recent[-1].id if recent else None
        revert_id = record_iteration(
            Iteration(
                deck_id=target.deck_id,
                deck_name=target.deck_name,
                bracket=target.bracket,
                parent_id=latest,
                audit_version="revert",
                audit_manifest={
                    "added": [],
                    "removed": [],
                    "rationale": f"Reverted to iteration {iteration_id} state.",
                    "audit_version": "revert",
                    "reverted_to_iteration_id": iteration_id,
                },
                deck_snapshot=target.deck_snapshot,
                verdict="kept",  # the revert action itself is "kept"
                verdict_notes=f"Reverted to iteration {iteration_id}.",
            ),
            db_path=db_path,
        )

    return RevertResult(
        iteration_id=iteration_id,
        restored_path=out_path,
        push_blob=blob,
        revert_iteration_id=revert_id,
        backup_path=backup_path,
    )


def revert_deck_to_version(
    deck_id: str,
    version: int,
    deck_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    record_revert: bool = True,
) -> RevertResult:
    """Revert a specific deck to its Nth recorded iteration. `version` is
    1-indexed; pass `1` for the original baseline."""
    history = iterations_for_deck(deck_id, db_path=db_path)
    if not history:
        raise ValueError(f"no iteration history for deck_id={deck_id!r}")
    if version < 1 or version > len(history):
        raise ValueError(
            f"version {version} out of range — deck has {len(history)} recorded iteration(s)"
        )
    target_iteration_id = history[version - 1].id
    if target_iteration_id is None:
        raise ValueError(f"iteration at index {version - 1} has no id")
    return revert_to_iteration(
        target_iteration_id,
        deck_path=deck_path,
        db_path=db_path,
        record_revert=record_revert,
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="commander-revert",
                                description="Roll back a deck to a logged iteration.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--to-iteration", type=int,
                   help="Iteration ID from knowledge_log to revert to.")
    g.add_argument("--to-deck", help="Deck publicId or filename to revert.")
    p.add_argument("--version", type=int, default=1,
                   help="When using --to-deck, which iteration version to restore (1-indexed).")
    p.add_argument("--no-clipboard", action="store_true",
                   help="Skip clipboard write of the Moxfield push blob.")
    p.add_argument("--no-record", action="store_true",
                   help="Don't record the revert as a new iteration.")
    args = p.parse_args(argv)

    try:
        if args.to_iteration is not None:
            result = revert_to_iteration(
                args.to_iteration,
                record_revert=not args.no_record,
            )
        else:
            result = revert_deck_to_version(
                args.to_deck,
                version=args.version,
                record_revert=not args.no_record,
            )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Reverted to iteration {result.iteration_id}")
    print(f"  Restored: {result.restored_path}")
    # Always tell the user where their previous state went — the whole point
    # of the backup is recoverability, and a backup nobody knows about is not
    # recoverable in practice.
    if result.backup_path:
        print(f"  Previous file backed up to: {result.backup_path}")
    else:
        print("  No backup needed (previous file was absent or identical to the snapshot)")
    if result.revert_iteration_id:
        print(f"  Revert recorded as iteration #{result.revert_iteration_id}")
    print()
    print("Push to Moxfield by pasting the textarea blob — generating now:")
    prepare_push(result.restored_path, copy_to_clipboard=not args.no_clipboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
