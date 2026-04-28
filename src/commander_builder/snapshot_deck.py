"""Snapshot a Forge `.dck` to a versioned filename.

Used at the start and end of an audit cycle so `compare_versions.py` has
distinct v1/v2 .dck files on disk:

    1. Before the audit: snapshot v1
       python -m commander_builder.snapshot_deck "[USER] My Deck [B3].dck" --version v1

    2. Run the Moxfield audit (separate Claude session with the v3 prompt)
       — the audit modifies the live Moxfield deck in place.

    3. Re-pull the post-audit deck via moxfield_import (overwrites the local file)
       python -m commander_builder.moxfield_import --user https://moxfield.com/decks/<id>

    4. After the audit: snapshot v2
       python -m commander_builder.snapshot_deck "[USER] My Deck [B3].dck" --version v2

    5. Compare
       python -m commander_builder.compare_versions \\
           --old "[USER] My Deck v1 [B3].dck" \\
           --new "[USER] My Deck v2 [B3].dck" \\
           --bracket 3 --games 10

The snapshot is a simple file copy with the version token inserted before
the bracket suffix so the resulting filename still ends with `[B<n>].dck`
and remains visible to Forge's deck picker.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from typing import Optional

from .forge_runner import VENDOR_FORGE

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"

# `[USER] Foo [B3].dck` → group 1 = "[USER] Foo", group 2 = " [B3].dck"
_BRACKET_TAIL = re.compile(r"^(.*?)(\s*\[B[0-9?]\]\.dck)$", re.IGNORECASE)


def versioned_path(deck_filename: str, version: str, base: Path = DECK_DIR) -> Path:
    """Return the on-disk path for a versioned snapshot.

    `[USER] Hakbal [B3].dck` + `v1` → `[USER] Hakbal v1 [B3].dck`.
    Decks without a `[B<n>]` suffix get the version appended before `.dck`.
    """
    m = _BRACKET_TAIL.match(deck_filename)
    if m:
        stem, tail = m.group(1), m.group(2)
        return base / f"{stem} {version}{tail}"
    if deck_filename.lower().endswith(".dck"):
        return base / f"{deck_filename[:-4]} {version}.dck"
    return base / f"{deck_filename} {version}.dck"


def snapshot(
    deck_filename: str,
    version: str,
    base: Path = DECK_DIR,
    overwrite: bool = False,
) -> Path:
    """Copy `<base>/deck_filename` to its versioned path. Returns the new path.

    Raises FileNotFoundError if the source isn't on disk, FileExistsError if
    the destination exists and `overwrite=False` (the default — re-snapshotting
    accidentally would clobber the prior baseline)."""
    src = base / deck_filename
    if not src.exists():
        raise FileNotFoundError(f"deck not found: {src}")
    dst = versioned_path(deck_filename, version, base)
    if dst.exists() and not overwrite:
        raise FileExistsError(
            f"snapshot exists: {dst}. Use --overwrite to replace it."
        )
    shutil.copy2(src, dst)
    return dst


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="snapshot_deck")
    p.add_argument("deck", help="Filename of the deck to snapshot (under commander/).")
    p.add_argument(
        "--version", default="v1",
        help="Version token to insert before the bracket suffix (default 'v1').",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="Replace an existing snapshot at the destination.",
    )
    args = p.parse_args(argv)
    try:
        out = snapshot(args.deck, args.version, overwrite=args.overwrite)
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Snapshot: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
