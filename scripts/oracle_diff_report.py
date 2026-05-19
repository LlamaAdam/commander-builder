#!/usr/bin/env python
"""Cross-reference Forge ``Oracle:`` text against Scryfall
``oracle_text`` for every card in the deck library, flag drift.

Catches the class of bug that today only surfaces as a wrong sim
verdict: WotC errata a card, Scryfall updates within days, but
the bundled Forge corpus lags. A sim that uses the stale text
produces incorrect game-state transitions and the iteration
verdict is wrong.

Output: one record per card that has BOTH a Forge script AND a
Scryfall payload, classified as ``match`` / ``differ`` /
``missing_forge`` / ``missing_scryfall`` / ``missing_both``.
Mismatches include the unified diff between normalized Forge
and normalized Scryfall texts so a maintainer can see what
needs reviewing at a glance.

Side effects: read-only against Forge + Scryfall (cache).
Scryfall lookups are disk-cached via ``scryfall_client``, so the
first run pays the cold cost once and re-runs are fast.

Usage:
    python scripts/oracle_diff_report.py                  # full library
    python scripts/oracle_diff_report.py --max-decks 10   # quick smoke
    python scripts/oracle_diff_report.py --only-mismatches
    python scripts/oracle_diff_report.py --json > drift.json
    python scripts/oracle_diff_report.py --diff           # show unified diffs

Tied to ``docs/AGENT_BACKLOG.md`` item #019.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Forge oracle text contains the actual Unicode minus sign (U+2212)
# in planeswalker loyalty costs (e.g. ``[-2]: ...``). Windows console
# defaults to cp1252 which can't encode it; reconfigure stdout to
# utf-8-with-replacement so the report renders cleanly cross-platform.
if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding and (
    "cp1252" in (sys.stdout.encoding or "").lower()
    or "ascii" in (sys.stdout.encoding or "").lower()
):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from commander_builder.deck_library_analyzer import (  # noqa: E402
    iter_deck_cards, iter_deck_files,
)
from commander_builder.forge_cards_loader import CardsLoader  # noqa: E402
from commander_builder.forge_script_parser import parse_card_script  # noqa: E402
from commander_builder.oracle_diff import compare_card_oracle  # noqa: E402
from commander_builder.scryfall_client import lookup_card  # noqa: E402

DEFAULT_DECK_DIR = REPO_ROOT / "vendor" / "forge" / "userdata" / "decks" / "commander"
DEFAULT_FORGE_DIR = REPO_ROOT / "vendor" / "forge"


# Pattern buckets help the maintainer triage 100s of diffs into
# 5 categories instead of scanning each one. Each bucket has a
# predicate that takes (normalized_forge, normalized_scryfall)
# → bool. A diff that matches no bucket is "other" — the genuinely
# interesting cases.
_DIFF_BUCKETS = [
    ("this-land errata", lambda f, s: "this land" in s.lower() and "this land" not in f.lower()),
    ("this-creature errata", lambda f, s: "this creature" in s.lower() and "this creature" not in f.lower()),
    ("this-artifact errata", lambda f, s: "this artifact" in s.lower() and "this artifact" not in f.lower()),
    ("this-enchantment errata", lambda f, s: "this enchantment" in s.lower() and "this enchantment" not in f.lower()),
    ("this-card errata", lambda f, s: "this card" in s.lower() and "this card" not in f.lower()),
    ("this-token errata", lambda f, s: "this token" in s.lower() and "this token" not in f.lower()),
    ("this-permanent errata", lambda f, s: "this permanent" in s.lower() and "this permanent" not in f.lower()),
]


def _categorize_diff(result) -> str:
    """Bucket a ``differ`` result into a coarse category. Returns
    ``other`` when no known pattern matches."""
    for label, predicate in _DIFF_BUCKETS:
        if predicate(result.normalized_forge, result.normalized_scryfall):
            return label
    return "other"


def _iter_distinct_cards(deck_dir: Path, max_decks: int | None):
    """Yield each distinct card name across the deck library exactly
    once. Walks .dck files in sorted order; dedupes on the way."""
    seen: set[str] = set()
    deck_files = list(iter_deck_files(deck_dir))
    if max_decks is not None:
        deck_files = deck_files[:max_decks]
    for deck_path in deck_files:
        try:
            text = deck_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _qty, name in iter_deck_cards(text):
            if name not in seen:
                seen.add(name)
                yield name


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
                   help="Cap scan to first N decks (sorted).")
    p.add_argument("--only-mismatches", action="store_true",
                   help="Hide ``match`` records; show only differ + missing_*.")
    p.add_argument("--diff", action="store_true",
                   help="Print the unified diff body inline (text mode).")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of human text.")
    p.add_argument("--by-pattern", action="store_true",
                   help="Group differs into pattern buckets "
                        "(this-land errata, this-creature errata, etc.) "
                        "in the human-readable output. Without this, "
                        "every diff prints individually.")
    args = p.parse_args(argv)

    if not args.deck_dir.is_dir():
        print(f"ERROR: deck dir not found: {args.deck_dir}", file=sys.stderr)
        return 2
    try:
        loader = CardsLoader.locate(args.forge_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    results = []
    counts = {"match": 0, "differ": 0, "missing_forge": 0,
              "missing_scryfall": 0, "missing_both": 0}
    try:
        for name in _iter_distinct_cards(args.deck_dir, args.max_decks):
            raw = loader.load_one(name)
            forge_card = parse_card_script(raw) if raw else None
            try:
                scryfall = lookup_card(name)
            except Exception:  # noqa: BLE001 — Scryfall blip != stop run
                scryfall = None
            result = compare_card_oracle(name, forge_card, scryfall)
            counts[result.status] = counts.get(result.status, 0) + 1
            if args.only_mismatches and result.match:
                continue
            results.append(result)
    finally:
        loader.close()

    if args.json:
        print(json.dumps({
            "counts": counts,
            "results": [r.to_dict() for r in results],
        }, indent=2))
        return 0

    print(f"Oracle-text drift scan ({loader.source.kind} corpus)")
    print(f"  match:            {counts.get('match', 0)}")
    print(f"  differ:           {counts.get('differ', 0)}")
    print(f"  missing_forge:    {counts.get('missing_forge', 0)}")
    print(f"  missing_scryfall: {counts.get('missing_scryfall', 0)}")
    print(f"  missing_both:     {counts.get('missing_both', 0)}")

    differs = [r for r in results if r.status == "differ"]
    if not differs:
        return 0

    if args.by_pattern:
        # Bucket view: 5-line summary instead of 200-line dump.
        from collections import defaultdict
        buckets: dict[str, list] = defaultdict(list)
        for r in differs:
            buckets[_categorize_diff(r)].append(r)
        print(f"\n=== {len(differs)} cards with text drift, by pattern ===")
        for label in sorted(buckets, key=lambda k: -len(buckets[k])):
            entries = buckets[label]
            print(f"\n  [{label}] {len(entries)} cards")
            for r in entries[:5]:
                print(f"    - {r.card_name}")
            if len(entries) > 5:
                print(f"    ... and {len(entries) - 5} more")
        return 0

    print(f"\n=== {len(differs)} cards with text drift ===")
    for r in differs:
        print(f"\n  [{r.card_name}]")
        if args.diff:
            for line in r.diff_lines:
                print(f"    {line}")
        else:
            # Compact one-line per side.
            print(f"    forge:    {r.normalized_forge[:160]}")
            print(f"    scryfall: {r.normalized_scryfall[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
