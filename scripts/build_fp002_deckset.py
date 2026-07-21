"""FP-002 deck-set builder -- grow the soak pool toward ~80+ unique decks.

For each commander in COMMANDERS: fetch the top-liked community (Moxfield)
build at its bracket, write it as a Forge ``[USER] <name> FP2 [B<n>].dck``
base deck, then run ``commander-auto-curate`` to produce the paired
``... FP2 v2ESC...`` curated deck. ``soak_pool --mode gauntlet`` discovers the
base + ` v2 ` pairs automatically and tests each against the fixed gauntlet.

Resumable: skips any base/v2 that already exists. Per-commander try/except so
one bad commander never kills the batch. ASCII-only output (cp1252 console).

Phases (per the FP-002 deck-generation plan in docs/future-plans.md):
  Phase 1 (acquire) + Phase 2 (curate) live here; the long Forge soak (Phase 3)
  is launched separately:

    python scripts/soak_pool.py --mode gauntlet --games 40 --append \
        --label Llama --out C:/Users/pilot/soak_inbox/Llama_gauntlet.jsonl

Usage:
  python scripts/build_fp002_deckset.py                 # all commanders
  python scripts/build_fp002_deckset.py --limit 2       # smoke test (first 2)
  python scripts/build_fp002_deckset.py --skip-curate   # base decks only
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from commander_builder.dck_meta import (
    rewrite_name_to_stem, stamp_name_preserving_display,
)
from commander_builder.forge_runner import VENDOR_FORGE
from commander_builder.moxfield_import import (
    find_top_liked_deck_for_commander, to_dck,
)

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"

# ~30 commanders spanning color identities and brackets B3/B4/B5. Diversity
# matters more than raw count (avoid 30 near-identical decks); these are all
# popular enough to have a top-liked Moxfield build to seed from.
COMMANDERS: list[tuple[str, int]] = [
    ("Krenko, Mob Boss", 4),
    ("Talrand, Sky Summoner", 3),
    ("Yawgmoth, Thran Physician", 4),
    ("Selvala, Heart of the Wilds", 4),
    ("Heliod, Sun-Crowned", 4),
    ("Atraxa, Praetors' Voice", 4),
    ("Edgar Markov", 4),
    ("Meren of Clan Nel Toth", 4),
    ("Kaalia of the Vast", 5),
    ("Niv-Mizzet, Parun", 4),
    ("Korvold, Fae-Cursed King", 5),
    ("Muldrotha, the Gravetide", 4),
    ("Chulane, Teller of Tales", 4),
    ("Ghave, Guru of Spores", 4),
    ("Yuriko, the Tiger's Shadow", 5),
    ("Kenrith, the Returned King", 4),
    ("The Ur-Dragon", 4),
    ("Prosper, Tome-Bound", 4),
    ("Miirym, Sentinel Wyrm", 4),
    ("Kinnan, Bonder Prodigy", 5),
    ("Winota, Joiner of Forces", 4),
    ("Tergrid, God of Fright", 4),
    ("Isshin, Two Heavens as One", 4),
    ("Wilhelt, the Rotcleaver", 4),
    ("Gishath, Sun's Avatar", 4),
    ("Omnath, Locus of Creation", 4),
    ("Kykar, Wind's Fury", 4),
    ("Sythis, Harvest's Hand", 3),
    ("Magda, Brazen Outlaw", 4),
    ("Aesi, Tyrant of Gyre Strait", 4),
]


def short(name: str) -> str:
    """'Korvold, Fae-Cursed King' -> 'Korvold'; keep it filename-clean."""
    base = re.sub(r",.*", "", name).strip()
    return re.sub(r"[^A-Za-z0-9 '-]", "", base)


def _base_path(deck_dir: Path, cmd: str, br: int) -> Path:
    return deck_dir / f"[USER] {short(cmd)} FP2 [B{br}].dck"


def _v2_path(deck_dir: Path, cmd: str, br: int) -> Path:
    # soak_pool pairs a base with the same name + ' v2 ' inserted before [B.
    return deck_dir / f"[USER] {short(cmd)} FP2 v2 [B{br}].dck"


def _fetch_base(cmd: str, br: int) -> dict | None:
    try:
        dj = find_top_liked_deck_for_commander(cmd, bracket=br)
    except Exception as exc:  # noqa: BLE001
        print(f"    fetch ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None
    if not dj:
        # retry without bracket filter (some commanders have few tagged decks)
        try:
            dj = find_top_liked_deck_for_commander(cmd)
        except Exception:  # noqa: BLE001
            dj = None
    return dj


def _adopt_v2(src: Path, v2: Path) -> None:
    """Rename a curator-named v2 into the soak-pair filename AND restamp
    its ``Name=`` to the new stem.

    WHY the restamp: the curator stamped ``Name=`` from the file's
    ORIGINAL stem at write time (dck_meta invariant: Name= == filename
    stem). A bare ``rename()`` silently breaks that invariant — Forge's
    deck picker locates a deck by the ``Name=`` matching the filename we
    pass on the sim command line, so a renamed-but-not-restamped v2 would
    be a deck the gauntlet can't even load. ``rewrite_name_to_stem`` is
    the canonical fix-after-rename helper (see dck_meta)."""
    src.rename(v2)
    rewrite_name_to_stem(v2)


def _curate(base: Path, br: int, source: str) -> bool:
    """Run commander-auto-curate to write the paired v2. Returns True on rc 0."""
    cmd = [
        "commander-auto-curate", str(base),
        "--bracket", str(br), "--source", source,
    ]
    print(f"    curate: {' '.join(cmd[:1])} ... --bracket {br} --source {source}",
          flush=True)
    try:
        rc = subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        # fall back to module entry if the console script isn't on PATH
        rc = subprocess.run(
            [sys.executable, "-m", "commander_builder._proposer_cli",
             str(base), "--bracket", str(br), "--source", source],
            check=False).returncode
    return rc == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_fp002_deckset")
    ap.add_argument("--deck-dir", type=Path, default=DECK_DIR,
                    help="where to write decks (default: the Forge pool dir)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N commanders (smoke test)")
    ap.add_argument("--source", default="claude",
                    help="auto-curate advisor source (default: claude)")
    ap.add_argument("--skip-curate", action="store_true",
                    help="write base decks only; skip the v2 curation step")
    args = ap.parse_args(argv)
    args.deck_dir.mkdir(parents=True, exist_ok=True)

    work = COMMANDERS[: args.limit] if args.limit else COMMANDERS
    made_base = made_v2 = skipped = failed = 0
    print(f"FP-002 deck-set build: {len(work)} commander(s) -> {args.deck_dir}",
          flush=True)
    for cmd, br in work:
        base = _base_path(args.deck_dir, cmd, br)
        v2 = _v2_path(args.deck_dir, cmd, br)
        print(f"[{cmd}] B{br}", flush=True)

        if base.exists():
            print(f"    base exists, skip fetch: {base.name}", flush=True)
        else:
            dj = _fetch_base(cmd, br)
            if not dj:
                print("    no popular deck found -> SKIP commander", flush=True)
                failed += 1
                continue
            # Stamp Name= from the FP2 filename stem. NOT for win
            # attribution — the gauntlet has attributed wins by SEAT, not
            # name, since e8777b6 — but Forge's own deck picker still
            # locates a deck by the Name= matching the filename we pass,
            # and to_dck's raw Moxfield name never matches
            # "[USER] <short> FP2 [Bn]" (see dck_meta).
            base.write_text(
                stamp_name_preserving_display(to_dck(dj), base.stem),
                encoding="utf-8",
            )
            made_base += 1
            print(f"    + {base.name}  (moxfield: {dj.get('name')!r})", flush=True)

        if args.skip_curate:
            continue
        if v2.exists():
            print(f"    v2 exists, skip curate: {v2.name}", flush=True)
            skipped += 1
            continue
        if _curate(base, br, args.source):
            if v2.exists():
                made_v2 += 1
                print(f"    + {v2.name}", flush=True)
            else:
                # curator may name the v2 differently; try to locate + rename
                stem = base.stem  # "[USER] X FP2 [B4]"
                cand = sorted(
                    p for p in args.deck_dir.glob("*.dck")
                    if " v2 " in p.name and p.name.startswith(stem.split(" [B")[0])
                    and f"[B{br}]" in p.name)
                if cand:
                    # Capture the source name BEFORE the rename — after
                    # _adopt_v2, cand[-1].name still prints the old string
                    # (Path is immutable) but keeping the read explicit
                    # avoids that trap biting a future edit.
                    src_name = cand[-1].name
                    _adopt_v2(cand[-1], v2)
                    made_v2 += 1
                    print(f"    + {v2.name}  (renamed from {src_name})",
                          flush=True)
                else:
                    print("    curate ran but no v2 found", flush=True)
                    failed += 1
        else:
            print("    curate FAILED (nonzero rc)", flush=True)
            failed += 1

    print(f"\nDONE: {made_base} base + {made_v2} v2 written, "
          f"{skipped} v2 skipped, {failed} failed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
