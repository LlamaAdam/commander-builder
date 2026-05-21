#!/usr/bin/env python
"""Bulk static-analysis of the vendored deck library against Forge.

Walks every ``.dck`` file under the Commander deck directory,
resolves each distinct card through Forge's card-script corpus
(``vendor/forge/res/cardsfolder/`` — auto-detects unzipped tree
vs. ``cardsfolder.zip``), parses with ``forge_script_parser``,
and prints aggregate counts: effect-kind histogram, keyword
histogram, ability-category histogram, SVar reference counts,
DeckHints frequency.

Use case from the 2026-05-19 conversation: "I feel #018 could
help looking over decks and working them out as well." Run this
to see which DSL effects dominate the library (informs which
Python-engine primitives would matter first), which Forge
SVar expressions repeat across decks, which DeckHints surface
archetype clusters, and which cards Forge doesn't ship a script
for (typos, new sets, custom cards).

Side effects: read-only against ``vendor/forge/`` and the deck
directory. Writes nothing.

Usage:
    python scripts/analyze_deck_library.py                # full library
    python scripts/analyze_deck_library.py --max-decks 10 # quick smoke
    python scripts/analyze_deck_library.py --json > report.json
    python scripts/analyze_deck_library.py --top 25       # cap histogram rows

Tied to ``docs/AGENT_BACKLOG.md`` item #018.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from commander_builder.deck_library_analyzer import (  # noqa: E402
    analyze_library,
)
from commander_builder.forge_cards_loader import CardsLoader  # noqa: E402

DEFAULT_DECK_DIR = REPO_ROOT / "vendor" / "forge" / "userdata" / "decks" / "commander"
DEFAULT_FORGE_DIR = REPO_ROOT / "vendor" / "forge"


def _print_section(title: str, counter: Counter | dict, top: int) -> None:
    items = (
        counter.most_common(top) if isinstance(counter, Counter)
        else list(counter.items())[:top]
    )
    print(f"\n=== {title} ({len(counter)} distinct) ===")
    if not items:
        print("  (none)")
        return
    width = max(len(str(k)) for k, _ in items)
    for k, v in items:
        print(f"  {k:<{width}}  {v}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--deck-dir", type=Path, default=DEFAULT_DECK_DIR,
        help=f"Commander deck directory (default {DEFAULT_DECK_DIR}).",
    )
    p.add_argument(
        "--forge-dir", type=Path, default=DEFAULT_FORGE_DIR,
        help=f"Forge install directory (default {DEFAULT_FORGE_DIR}).",
    )
    p.add_argument("--max-decks", type=int, default=None,
                   help="Cap scan to first N decks (sorted). For quick smoke runs.")
    p.add_argument("--top", type=int, default=25,
                   help="Show top N entries per histogram (default 25).")
    p.add_argument("--per-deck", action="store_true",
                   help="Include per-deck card breakdown in the JSON output.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of human-readable text.")
    args = p.parse_args(argv)

    if not args.deck_dir.is_dir():
        print(f"ERROR: deck dir not found: {args.deck_dir}", file=sys.stderr)
        return 2
    try:
        loader = CardsLoader.locate(args.forge_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        report = analyze_library(
            args.deck_dir, loader,
            max_decks=args.max_decks,
            include_per_deck=args.per_deck,
        )
    finally:
        loader.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    print(f"Deck library analyzer ({loader.source.kind} corpus at {loader.source.path})")
    print(f"  Decks scanned:    {report.decks_scanned}")
    print(f"  Distinct cards:   {report.distinct_cards}")
    print(f"  Resolved:         {report.resolved_cards}")
    print(f"  Unresolved:       {len(report.unresolved_cards)}")
    _print_section("Effect kinds (AB$/SP$/Mode$ ...)", report.effect_kinds, args.top)
    _print_section("Ability categories", report.ability_categories, args.top)
    _print_section("Keywords (K: lines)", report.keywords, args.top)
    _print_section("SVar references", report.svar_names, args.top)
    _print_section("DeckHints", report.deck_hints, args.top)
    _print_section("DeckHas", report.deck_has, args.top)
    if report.unresolved_cards:
        print(f"\n=== Unresolved cards (sample, {len(report.unresolved_cards)} total) ===")
        for name in report.unresolved_cards[:args.top]:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
