"""FP-015 tier-3 validation — does CardScore ranking beat bucket order?

THE question the flag is gated on (docs/future-plans.md, FP-015 "How
this gets validated", tier 3): take the SAME deck and the SAME budget
of k swaps, build one proposal from the advisor's current bucket-order
ranking (flag off) and one from the CardScore ranking (flag on), then
A/B sim EACH proposal against the original deck at equal game counts
through the existing ``compare_versions`` harness. If the score-ranked
arm's win-rate margin beats the bucket-ordered arm's across decks, the
formula earns its default-on; if not, it stays a flag.

This is deliberately NOT a regression on margin — FP-002 established
no pre-sim feature clears |t| >= 2 at n=45, and a card scorer would
fail that bar uninformatively. Two margins on identical budgets is a
direct, readable comparison.

Usage::

    python scripts/validate_card_score.py DECK.dck [DECK2.dck ...]
        [--bracket 3] [--k 5] [--games 40] [--dry-run] [--json]

``--dry-run`` stops before any Forge game and just prints the two swap
sets — use it to sanity-check the arms differ before paying sim time.
Every Forge-facing step reuses shipped machinery (``advise()``,
``_apply_swaps_to_dck``, ``compare_versions.compare``); this script
only orchestrates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from commander_builder.card_score import CARD_SCORE_ENV_VAR  # noqa: E402


def build_arm_swaps(
    deck_path: Path,
    bracket: int,
    k: int,
    flag_on: bool,
    advise_fn: Optional[Callable] = None,
) -> tuple[list[str], list[str]]:
    """Top-k adds + cuts from the advisor under one flag setting.

    Toggles ``COMMANDER_BUILDER_CARD_SCORE`` around the call so the
    REAL production ranking paths are exercised — that is the point of
    tier 3: validate what ships, not a reimplementation. The env var is
    always restored, even on failure.
    """
    if advise_fn is None:
        from commander_builder.improvement_advisor import advise as advise_fn
    prior = os.environ.get(CARD_SCORE_ENV_VAR)
    os.environ[CARD_SCORE_ENV_VAR] = "1" if flag_on else "0"
    try:
        report = advise_fn(deck_path, bracket)
    finally:
        if prior is None:
            os.environ.pop(CARD_SCORE_ENV_VAR, None)
        else:
            os.environ[CARD_SCORE_ENV_VAR] = prior
    adds = [r.card for r in report.recommendations if r.action == "add"][:k]
    cuts = [r.card for r in report.recommendations if r.action == "cut"][:k]
    return adds, cuts


def stage_arm(
    original_text: str,
    adds: list[str],
    cuts: list[str],
    out_path: Path,
) -> bool:
    """Apply one arm's swaps through the shared legality path and write
    the staged ``.dck``. Returns False when the swaps were a no-op
    (nothing applied — no point simming an identical deck)."""
    from commander_builder._advisor_models import SwapRecommendation
    from commander_builder.web.deck_text_ops import _apply_swaps_to_dck
    recs = ([SwapRecommendation(card=c, action="add", reason="tier3")
             for c in adds]
            + [SwapRecommendation(card=c, action="cut", reason="tier3")
               for c in cuts])
    proposed, applied_adds, applied_cuts, _kept = _apply_swaps_to_dck(
        original_text, recs,
    )
    if not applied_adds and not applied_cuts:
        return False
    out_path.write_text(proposed, encoding="utf-8")
    return True


def run_deck(
    deck_path: Path,
    bracket: int,
    k: int,
    games: int,
    stage_dir: Path,
    dry_run: bool = False,
    advise_fn: Optional[Callable] = None,
    compare_fn: Optional[Callable] = None,
) -> dict:
    """One deck's tier-3 row: build both arms, stage, sim, report."""
    original_text = deck_path.read_text(encoding="utf-8")
    bucket_adds, bucket_cuts = build_arm_swaps(
        deck_path, bracket, k, flag_on=False, advise_fn=advise_fn)
    score_adds, score_cuts = build_arm_swaps(
        deck_path, bracket, k, flag_on=True, advise_fn=advise_fn)

    row: dict = {
        "deck": deck_path.name,
        "bracket": bracket,
        "k": k,
        "bucket_arm": {"adds": bucket_adds, "cuts": bucket_cuts},
        "score_arm": {"adds": score_adds, "cuts": score_cuts},
        "arms_identical": (bucket_adds == score_adds
                           and bucket_cuts == score_cuts),
    }
    if row["arms_identical"]:
        row["skipped"] = "arms identical — no signal to measure"
        return row
    if dry_run:
        row["skipped"] = "dry run"
        return row

    stage_dir.mkdir(parents=True, exist_ok=True)
    stem = deck_path.stem
    arm_margins: dict[str, Optional[float]] = {}
    if compare_fn is None:
        from commander_builder.compare_versions import compare as compare_fn
    for arm, adds, cuts in (("bucket", bucket_adds, bucket_cuts),
                            ("score", score_adds, score_cuts)):
        staged = stage_dir / f"{stem}__tier3_{arm}.dck"
        if not stage_arm(original_text, adds, cuts, staged):
            arm_margins[arm] = None
            continue
        # Stage the original beside it so compare() resolves both from
        # one deck_dir.
        original_copy = stage_dir / f"{stem}__tier3_base.dck"
        original_copy.write_text(original_text, encoding="utf-8")
        report = compare_fn(
            old_deck=original_copy.name,
            new_deck=staged.name,
            bracket=bracket,
            games_per_pod=games,
            deck_dir=stage_dir,
        )
        old_wins = report.old_stats.wins
        new_wins = report.new_stats.wins
        decisive = old_wins + new_wins
        arm_margins[arm] = ((new_wins - old_wins) / decisive
                            if decisive else None)
        row[f"{arm}_sim"] = {"old_wins": old_wins, "new_wins": new_wins,
                             "games": report.old_stats.games}
    row["bucket_margin"] = arm_margins.get("bucket")
    row["score_margin"] = arm_margins.get("score")
    if (row["bucket_margin"] is not None
            and row["score_margin"] is not None):
        row["winner"] = ("score" if row["score_margin"]
                         > row["bucket_margin"]
                         else "bucket" if row["bucket_margin"]
                         > row["score_margin"] else "tie")
    return row


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FP-015 tier-3: CardScore ranking vs bucket order, "
                    "both A/B simmed at equal budgets.")
    parser.add_argument("decks", nargs="+", help=".dck paths")
    parser.add_argument("--bracket", type=int, default=3)
    parser.add_argument("--k", type=int, default=5,
                        help="swap budget per arm (default %(default)s)")
    parser.add_argument("--games", type=int, default=40,
                        help="games per pod per arm (default %(default)s)")
    parser.add_argument("--stage-dir", default=None,
                        help="where staged arm decks go (default: "
                             "_tier3_stage next to the first deck)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    # advise() resolves RELATIVE paths against its own DECK_DIR, which
    # would double the prefix for paths already inside it — hand it
    # absolutes only.
    deck_paths = [Path(d).resolve() for d in args.decks]
    missing = [p for p in deck_paths if not p.is_file()]
    if missing:
        print(f"error: no such deck(s): {missing}", file=sys.stderr)
        return 2
    stage_dir = (Path(args.stage_dir) if args.stage_dir
                 else deck_paths[0].parent / "_tier3_stage")

    rows = []
    for p in deck_paths:
        print(f"[tier3] {p.name} ...", file=sys.stderr, flush=True)
        rows.append(run_deck(p, args.bracket, args.k, args.games,
                             stage_dir, dry_run=args.dry_run))

    summary = {
        "rows": rows,
        "decks": len(rows),
        "score_wins": sum(1 for r in rows if r.get("winner") == "score"),
        "bucket_wins": sum(1 for r in rows if r.get("winner") == "bucket"),
        "ties": sum(1 for r in rows if r.get("winner") == "tie"),
        "skipped": sum(1 for r in rows if r.get("skipped")),
    }
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        for r in rows:
            if r.get("skipped"):
                print(f"  {r['deck']}: SKIPPED ({r['skipped']})")
                if r.get("arms_identical") is False and args.dry_run:
                    print(f"    bucket arm: +{r['bucket_arm']['adds']} "
                          f"-{r['bucket_arm']['cuts']}")
                    print(f"    score  arm: +{r['score_arm']['adds']} "
                          f"-{r['score_arm']['cuts']}")
            else:
                print(f"  {r['deck']}: bucket {r['bucket_margin']} vs "
                      f"score {r['score_margin']} -> {r.get('winner')}")
        print(f"score {summary['score_wins']} / bucket "
              f"{summary['bucket_wins']} / tie {summary['ties']} / "
              f"skipped {summary['skipped']} over {summary['decks']} decks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
