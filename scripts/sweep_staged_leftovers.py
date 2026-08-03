#!/usr/bin/env python
"""Delete leaked staged validation decks from a deck dir.

The tier-3 / per-swap harnesses stage ``<stem>__tier3_*.dck`` and
``<stem>__perswap_*.dck`` copies in the REAL Forge deck dir and remove
them afterwards. If a historical run leaked them (the pre-fix cleanup
used a stem-based glob, and real stems contain [USER]/[B3] — square
brackets are character classes to pathlib.glob AND to shells, so the
glob matched nothing), this recovers without hand-typed shell globs.

Dry-run by default: prints what it would delete. Pass --delete to
actually remove the files. Matching is plain substring on the filename
— no glob anywhere, so bracketed stems are handled correctly.
"""

from __future__ import annotations

import argparse
from pathlib import Path

MARKERS = ("__tier3_", "__perswap_")


def find_leftovers(deck_dir: Path) -> list[Path]:
    return sorted(p for p in deck_dir.iterdir()
                  if p.is_file() and p.suffix == ".dck"
                  and any(m in p.name for m in MARKERS))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="delete leaked *__tier3_* / *__perswap_* staged "
                    "decks from a deck dir (dry run by default)")
    parser.add_argument("deck_dir", type=Path)
    parser.add_argument("--delete", action="store_true",
                        help="actually delete (default: dry run)")
    args = parser.parse_args(argv)
    if not args.deck_dir.is_dir():
        parser.error(f"no such directory: {args.deck_dir}")
    hits = find_leftovers(args.deck_dir)
    verb = "deleted" if args.delete else "would delete"
    for p in hits:
        print(f"{verb} {p.name}")
        if args.delete:
            p.unlink()
    tail = "" if args.delete else " (dry run — pass --delete to remove)"
    print(f"{len(hits)} staged leftover(s){tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
