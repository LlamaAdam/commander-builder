"""Generate known-bad "control" decks for pipeline calibration.

The base-vs-v2 soak compares *similar* decks, so a single verdict is
noise-dominated and you can't tell a real signal from chance. Control
decks fix that: a deliberately-broken deck with a LARGE, known-direction
outcome. The headline control is a **do-nothing deck** — keep a real
commander but make the 99-card main all ``Wastes`` (colorless basics,
legal under any color identity). It can't even cast a colored commander,
so it does nothing and should lose ~every game.

Pairing a good deck against a control is a calibration canary: if the
harness can't show the good deck crushing an obviously-broken one, the
*measurement* is suspect (see calibration_check.py).

Usage:
  python scripts/make_control_decks.py            # build defaults into the deck dir
  python scripts/make_control_decks.py --source "<deck.dck>" --out "<control.dck>"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from commander_builder.forge_runner import VENDOR_FORGE

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def _commander_block(deck_text: str) -> list[str]:
    """Extract the ``[Commander]`` section's card lines from a .dck."""
    lines = deck_text.splitlines()
    out: list[str] = []
    in_cmd = False
    for ln in lines:
        s = ln.strip()
        if s.lower() == "[commander]":
            in_cmd = True
            continue
        if in_cmd:
            if s.startswith("["):
                break
            if s:
                out.append(s)
    return out


def make_do_nothing(source_text: str, name: str) -> str:
    """Build a do-nothing control: source's commander + 99 Wastes main."""
    cmd = _commander_block(source_text)
    if not cmd:
        raise ValueError("source deck has no [Commander] section")
    return (
        "[metadata]\n"
        f"Name={name}\n"
        "[Commander]\n"
        + "\n".join(cmd) + "\n"
        "[Main]\n"
        "99 Wastes\n"
    )


def _bracket(name: str) -> str:
    import re
    m = re.search(r"\[B\d\]", name)
    return m.group(0) if m else "[B3]"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_control_decks")
    ap.add_argument("--source", default=None,
                    help="Source .dck to borrow a commander from. Omit to "
                         "build the default set from a few existing decks.")
    ap.add_argument("--out", default=None, help="Output path (with --source).")
    ap.add_argument("--deck-dir", type=Path, default=DECK_DIR)
    args = ap.parse_args(argv)

    if args.source:
        src = Path(args.source)
        out = Path(args.out) if args.out else (
            args.deck_dir / f"[CONTROL] do-nothing {_bracket(src.name)}.dck")
        out.write_text(make_do_nothing(src.read_text(encoding="utf-8"),
                                       f"CONTROL do-nothing {_bracket(src.name)}"),
                       encoding="utf-8")
        print(f"wrote {out.name}")
        return 0

    # Default set: one do-nothing control per bracket we have decks for,
    # borrowing a real commander so the deck loads in Forge.
    made = 0
    seen_brackets: set[str] = set()
    for p in sorted(args.deck_dir.glob("[[]USER[]]*.dck")):
        if " v2 " in p.name or "-detune" in p.name or "SPDET" in p.name:
            continue
        b = _bracket(p.name)
        if b in seen_brackets:
            continue
        seen_brackets.add(b)
        out = args.deck_dir / f"[CONTROL] do-nothing {b}.dck"
        out.write_text(
            make_do_nothing(p.read_text(encoding="utf-8"),
                            f"CONTROL do-nothing {b}"),
            encoding="utf-8")
        print(f"wrote {out.name}  (commander from {p.name})")
        made += 1
    print(f"created {made} control deck(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
