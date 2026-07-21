"""Pull the most-liked community Moxfield build for each of our commanders.

For every [USER] base deck, look up that commander's top-liked Moxfield
deck (at the same bracket) and write it as a Forge .dck. These are the
"good other person build" leg of the 4-build matrix: your original, this
popular build, and a curated fix of each.

Output names use the [USER] ... POP [B?] convention so soak_pool pairs
them (it keys on "[USER]" + " v2 "), and a curator can later write
"[USER] <name> POP v2 [B?].dck" as the fix. Default output is the shared
inbox's popular_decks/ so the curator machine (box2) can pull them.

Usage:
  python scripts/pull_popular_decks.py
  python scripts/pull_popular_decks.py --out C:/Users/pilot/soak_inbox/popular_decks
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from commander_builder.dck_meta import stamp_name_preserving_display
from commander_builder.forge_runner import VENDOR_FORGE
from commander_builder.moxfield_import import (
    find_top_liked_deck_for_commander, to_dck,
)
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def commander_of(deck_text: str) -> str | None:
    in_cmd = False
    for ln in deck_text.splitlines():
        s = ln.strip()
        if s.lower() == "[commander]":
            in_cmd = True
            continue
        if in_cmd:
            if s.startswith("["):
                break
            m = re.match(r"^\d+\s+(.*)$", s)
            if m:
                return m.group(1).split("|")[0].strip()
    return None


def short(name: str) -> str:
    # "Zhulodok, Void Gorger" -> "Zhulodok"; keep it filename-clean.
    base = re.sub(r",.*", "", name).strip()
    return re.sub(r"[^A-Za-z0-9 '-]", "", base)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pull_popular_decks")
    ap.add_argument("--out", type=Path,
                    default=Path("C:/Users/pilot/soak_inbox/popular_decks"))
    ap.add_argument("--source-dir", type=Path, default=DECK_DIR)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    bases = [p for p in sorted(args.source_dir.glob("[[]USER[]]*.dck"))
             if " v2 " not in p.name and "-detune" not in p.name
             and "SPDET" not in p.name and "POP" not in p.name]
    made = failed = 0
    for p in bases:
        cmd = commander_of(p.read_text(encoding="utf-8"))
        br = _bracket_from_filename(p.name) or 3
        if not cmd:
            print(f"  skip (no commander): {p.name}")
            continue
        print(f"  [{cmd}] B{br} ...", flush=True)
        try:
            dj = find_top_liked_deck_for_commander(cmd, bracket=br)
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if not dj:
            # Retry without the bracket filter (some commanders have few
            # bracket-tagged decks but plenty untagged).
            try:
                dj = find_top_liked_deck_for_commander(cmd)
            except Exception:  # noqa: BLE001
                dj = None
        if not dj:
            print("    no popular deck found")
            failed += 1
            continue
        out = args.out / f"[USER] {short(cmd)} POP [B{br}].dck"
        # Name= must match the filename stem or Forge/win-attribution can't
        # map the deck back to this file (to_dck stamps the raw Moxfield
        # name; the POP filename never matches it). Same invariant as the
        # library importers — see dck_meta.
        out.write_text(
            stamp_name_preserving_display(to_dck(dj), out.stem),
            encoding="utf-8",
        )
        made += 1
        print(f"    + {out.name}  (moxfield: {dj.get('name')!r})")
    print(f"\npulled {made} popular deck(s), {failed} missing -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
