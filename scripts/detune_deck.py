"""Detune a deck: swap its strongest cards for basic lands.

Purpose: the curation knowledge_log is skewed -- every proposal on an
already-tuned deck loses or ties the Forge A/B sim ("reverted"/"neutral"),
so there are zero "kept" (winning-swap) examples to train an FP-002
classifier. Detuning manufactures positive examples: weaken a good deck,
then run commander-auto-curate on the detuned variant -- the curator's
restorative swaps should beat the crippled deck in sim -> "kept".

Strategy: remove N cards (game-changers first, since those are the deck's
real power; then other non-basic single-copies) and add N basic lands
(distributed across the basic types already in the deck). Card count is
preserved, color identity is unchanged (we only add colors already present),
so the deck stays legal for Forge.

Usage:
    python scripts/detune_deck.py "<deck.dck>" --out "<detuned.dck>" -n 12
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from commander_builder.game_changers import load_game_changers

_BASICS = {"Forest", "Island", "Swamp", "Mountain", "Plains", "Wastes"}


def _card_name(line: str) -> str:
    """'1 Cyclonic Rift|2XM|47' -> 'Cyclonic Rift'."""
    body = line.strip()
    m = re.match(r"^\d+\s+(.*)$", body)
    if not m:
        return ""
    return m.group(1).split("|")[0].strip()


def _card_count(line: str) -> int:
    m = re.match(r"^(\d+)\s+", line.strip())
    return int(m.group(1)) if m else 1


def _is_basic(name: str) -> bool:
    return name.replace("Snow-Covered ", "") in _BASICS


def detune(deck_text: str, n: int = 12, seed: int | None = None) -> tuple[str, list[str], int]:
    """Return (detuned_text, removed_card_names, n_basics_added).

    With a ``seed``, the removal selection is shuffled WITHIN power tiers
    (game-changers first, then other non-basics) so repeated detunes of the
    same deck remove a varied mix -- giving diverse positive examples -- while
    still biasing toward strong cards so the original reliably beats the
    detuned version in sim.
    """
    import random
    rng = random.Random(seed)
    lines = deck_text.splitlines()

    # Locate [Main] section bounds.
    main_start = main_end = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "[main]":
            main_start = i + 1
        elif main_start is not None and ln.strip().startswith("[") and main_end is None:
            main_end = i
    if main_start is None:
        raise ValueError("no [Main] section")
    if main_end is None:
        main_end = len(lines)

    main = lines[main_start:main_end]

    gc = load_game_changers()

    # Candidate removals: single-copy, non-basic cards. Game-changers first.
    removable_idx = [
        i for i, ln in enumerate(main)
        if ln.strip() and _card_count(ln) == 1 and not _is_basic(_card_name(ln))
    ]
    # Two power tiers: game-changers, then everything else. Shuffle within
    # each tier (seeded) for variety, then concatenate so GC are still
    # preferred for removal (keeps the power gap large enough to win).
    gc_tier = [i for i in removable_idx if _card_name(main[i]) in gc]
    other_tier = [i for i in removable_idx if _card_name(main[i]) not in gc]
    rng.shuffle(gc_tier)
    rng.shuffle(other_tier)
    removable_idx = gc_tier + other_tier

    # Existing basic-land print lines (to know how to pad mana).
    basic_lines = [i for i, ln in enumerate(main)
                   if ln.strip() and _is_basic(_card_name(ln))]
    if not basic_lines:
        # Nonbasic-heavy manabase (no basics to bump). Add a Wastes line —
        # Wastes is a colorless basic, legal in EVERY Commander deck (no
        # color-identity violation) — and pad into it. Keeps detune working
        # for fetch/dual-only decks instead of erroring.
        main.append("0 Wastes")
        basic_lines = [len(main) - 1]

    to_remove = removable_idx[:n]
    removed_names = [_card_name(main[i]) for i in to_remove]
    n_removed = len(to_remove)

    # Remove (mark None), then distribute n_removed extra basics across the
    # existing basic-land lines round-robin (bump their counts).
    for i in to_remove:
        main[i] = None  # type: ignore
    for k in range(n_removed):
        bi = basic_lines[k % len(basic_lines)]
        cnt = _card_count(main[bi])
        rest = main[bi].strip()[len(str(cnt)):].lstrip()
        main[bi] = f"{cnt + 1} {rest}"

    new_main = [ln for ln in main if ln is not None]
    out = lines[:main_start] + new_main + lines[main_end:]
    return "\n".join(out) + "\n", removed_names, n_removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("--out", required=True)
    ap.add_argument("-n", type=int, default=12)
    args = ap.parse_args()

    text = Path(args.deck).read_text(encoding="utf-8")
    detuned, removed, n_added = detune(text, n=args.n)
    Path(args.out).write_text(detuned, encoding="utf-8")
    print(f"detuned {Path(args.deck).name}")
    print(f"  removed {len(removed)} cards, added {n_added} basics")
    print(f"  removed: {', '.join(removed)}")
    print(f"  wrote: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
