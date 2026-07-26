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

Arms
----
``--arms`` selects which rankings to put on the table. Each named arm
is staged and A/B simmed against the SAME original deck:

- ``bucket`` — the advisor's current bucket-order ranking (flag off).
  The control arm.
- ``score``  — the CardScore ranking (flag on). Tier 3 proper.
- ``bubble`` — the flag-on ranking passed through the whole-deck
  verdict (``bubble_analysis.apply_verdict_to_report``): the deck's own
  change budget decides HOW MANY swaps, and cuts are ordered
  bubble-first. This is the FP-015 addendum's open slice.

When ``bubble`` is in the mix it sets the budget for every other arm —
each arm is capped to the same number of adds and cuts the verdict
allowed. Otherwise the comparison would conflate "fewer swaps" with
"better-chosen swaps", and only the second is a claim about ranking.

Usage::

    python scripts/validate_card_score.py DECK.dck [DECK2.dck ...]
        [--bracket 3] [--k 5] [--games 40] [--arms bucket,score]
        [--dry-run] [--json]

``--dry-run`` stops before any Forge game and just prints each arm's
swap set — use it to sanity-check the arms differ before paying sim
time. Every Forge-facing step reuses shipped machinery (``advise()``,
``apply_verdict_to_report``, ``_apply_swaps_to_dck``,
``compare_versions.compare``); this script only orchestrates.
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


ARMS = ("bucket", "score", "bubble")


def _advise_under_flag(
    deck_path: Path,
    bracket: int,
    flag_on: bool,
    advise_fn: Optional[Callable] = None,
):
    """Run ``advise()`` with the card-score flag pinned on or off.

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
        return advise_fn(deck_path, bracket)
    finally:
        if prior is None:
            os.environ.pop(CARD_SCORE_ENV_VAR, None)
        else:
            os.environ[CARD_SCORE_ENV_VAR] = prior


def _swaps_from(report, k: int) -> tuple[list[str], list[str]]:
    adds = [r.card for r in report.recommendations if r.action == "add"][:k]
    cuts = [r.card for r in report.recommendations if r.action == "cut"][:k]
    return adds, cuts


def build_arm_swaps(
    deck_path: Path,
    bracket: int,
    k: int,
    flag_on: bool,
    advise_fn: Optional[Callable] = None,
) -> tuple[list[str], list[str]]:
    """Top-k adds + cuts from the advisor under one flag setting."""
    return _swaps_from(
        _advise_under_flag(deck_path, bracket, flag_on, advise_fn), k)


def build_bubble_arm_swaps(
    deck_path: Path,
    bracket: int,
    k: int,
    advise_fn: Optional[Callable] = None,
    verdict_fn: Optional[Callable] = None,
    corpus_fn: Optional[Callable] = None,
) -> tuple[list[str], list[str], dict]:
    """The flag-on ranking after the whole-deck verdict pass.

    Mirrors what ``commander-advise`` does when the flag is on: build
    the reference corpus for the commander, then hand the report to
    ``apply_verdict_to_report``, which trims to the deck's change
    budget and reorders cuts bubble-first. Returns the trimmed swaps
    plus an info dict (verdict / budget / bubble cards) for the row.

    Unlike the CLI, failures are NOT swallowed here: a fail-quiet
    verdict would silently degrade this arm into a duplicate of the
    score arm and the comparison would read as "no difference".
    """
    report = _advise_under_flag(deck_path, bracket, True, advise_fn)
    if verdict_fn is None:
        from commander_builder.bubble_analysis import (
            apply_verdict_to_report as verdict_fn,
        )
    if corpus_fn is None:
        from commander_builder.bubble_analysis import build_reference_corpus

        def corpus_fn(commander, bracket):
            return build_reference_corpus(commander, bracket=bracket)

    commanders = list(getattr(report, "commander_names", None) or [])
    corpus = corpus_fn(" // ".join(commanders), bracket) if commanders else None
    trimmed = verdict_fn(
        report,
        deck_text=Path(deck_path).read_text(encoding="utf-8"),
        corpus=corpus,
        bracket=bracket,
    )
    deck_score = dict(getattr(trimmed, "deck_score", None) or {})
    info = {
        "verdict": deck_score.get("verdict"),
        "change_budget": deck_score.get("change_budget"),
        "deck_score": deck_score.get("score"),
        "bubble_cards": [str(b.get("card", "")) for b
                         in (getattr(trimmed, "bubble_cards", None) or [])],
    }
    adds, cuts = _swaps_from(trimmed, k)
    return adds, cuts, info


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
    from commander_builder.dck_meta import rewrite_name
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
    # Name= MUST match the staged filename stem — log_parser attributes
    # wins by Forge's displayed deck name, and base + arm sharing the
    # original Name= would resurrect the pre-e8777b6 attribution bug
    # (caught live: first pilot parsed 0 games).
    out_path.write_text(rewrite_name(proposed, out_path.stem),
                        encoding="utf-8")
    return True


def _build_arms(
    deck_path: Path,
    bracket: int,
    k: int,
    arms: tuple[str, ...],
    advise_fn: Optional[Callable] = None,
    verdict_fn: Optional[Callable] = None,
    corpus_fn: Optional[Callable] = None,
) -> tuple[dict[str, tuple[list[str], list[str]]], Optional[dict]]:
    """Each arm's (adds, cuts), plus the bubble arm's verdict info.

    When the bubble arm is present its budget is authoritative: every
    other arm is truncated to the same add/cut counts so the arms
    differ only in WHICH cards they pick, never in how many.
    """
    built: dict[str, tuple[list[str], list[str]]] = {}
    info: Optional[dict] = None
    for arm in arms:
        if arm == "bubble":
            adds, cuts, info = build_bubble_arm_swaps(
                deck_path, bracket, k, advise_fn=advise_fn,
                verdict_fn=verdict_fn, corpus_fn=corpus_fn)
        else:
            adds, cuts = build_arm_swaps(
                deck_path, bracket, k, flag_on=(arm == "score"),
                advise_fn=advise_fn)
        built[arm] = (adds, cuts)
    if "bubble" in built:
        n_adds, n_cuts = (len(built["bubble"][0]), len(built["bubble"][1]))
        built = {arm: (adds[:n_adds], cuts[:n_cuts])
                 for arm, (adds, cuts) in built.items()}
    return built, info


def run_deck(
    deck_path: Path,
    bracket: int,
    k: int,
    games: int,
    stage_dir: Path,
    dry_run: bool = False,
    advise_fn: Optional[Callable] = None,
    compare_fn: Optional[Callable] = None,
    arms: tuple[str, ...] = ("bucket", "score"),
    verdict_fn: Optional[Callable] = None,
    corpus_fn: Optional[Callable] = None,
) -> dict:
    """One deck's tier-3 row: build each arm, stage, sim, report."""
    original_text = deck_path.read_text(encoding="utf-8")
    built, verdict_info = _build_arms(
        deck_path, bracket, k, arms, advise_fn=advise_fn,
        verdict_fn=verdict_fn, corpus_fn=corpus_fn)

    swap_sets = list(built.values())
    row: dict = {
        "deck": deck_path.name,
        "bracket": bracket,
        "k": k,
        "arms": list(arms),
        "arms_identical": all(s == swap_sets[0] for s in swap_sets),
    }
    for arm, (adds, cuts) in built.items():
        row[f"{arm}_arm"] = {"adds": adds, "cuts": cuts}
    if verdict_info is not None:
        row["bubble_verdict"] = verdict_info
        b_adds, b_cuts = built["bubble"]
        row["budget"] = {"adds": len(b_adds), "cuts": len(b_cuts)}
        if not b_adds and not b_cuts:
            # "overhaul" (or a keep verdict with a 0 budget): the deck's
            # own verdict says swaps aren't the fix. Simming an unchanged
            # deck against itself would burn hours to measure noise.
            row["skipped"] = "verdict allowed a 0-swap budget"
            return row
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
    for arm, (adds, cuts) in built.items():
        staged = stage_dir / f"{stem}__tier3_{arm}.dck"
        if not stage_arm(original_text, adds, cuts, staged):
            arm_margins[arm] = None
            continue
        # Stage the original beside it so compare() resolves both from
        # one deck_dir — with its Name= rewritten to the staged stem for
        # the same attribution reason as the arm deck.
        from commander_builder.dck_meta import rewrite_name
        original_copy = stage_dir / f"{stem}__tier3_base.dck"
        original_copy.write_text(
            rewrite_name(original_text, original_copy.stem),
            encoding="utf-8")
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
    # The staged decks live in the REAL deck dir (Forge requirement) —
    # remove them so they never pollute the deck list / web UI. The
    # persisted compare reports remain the durable record.
    for leftover in stage_dir.glob(f"{stem}__tier3_*.dck"):
        try:
            leftover.unlink()
        except OSError:
            pass
    for arm in built:
        row[f"{arm}_margin"] = arm_margins.get(arm)
    # A winner needs EVERY arm measured: a None margin means that arm
    # never got a real sim, and declaring a winner over a missing
    # opponent is exactly the kind of unearned conclusion this harness
    # exists to avoid.
    measured = {arm: m for arm, m in arm_margins.items() if m is not None}
    if len(measured) == len(built) and measured:
        best = max(measured.values())
        leaders = [arm for arm, m in measured.items() if m == best]
        row["winner"] = leaders[0] if len(leaders) == 1 else "tie"
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
    parser.add_argument("--arms", default="bucket,score",
                        help="comma-separated arms to compare, any of "
                             f"{','.join(ARMS)} (default %(default)s). "
                             "'bubble' runs the flag-on ranking through "
                             "the whole-deck verdict and its budget then "
                             "caps every other arm.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", default=None,
                        help="also write the summary JSON to this file "
                             "(clean — compare()'s progress lines go to "
                             "stdout, so redirecting stdout is NOT a "
                             "reliable way to capture the JSON)")
    args = parser.parse_args(argv)

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    unknown = [a for a in arms if a not in ARMS]
    if unknown or len(arms) < 2:
        print(f"error: unknown arm(s) {unknown}" if unknown
              else "error: need at least two arms to compare",
              file=sys.stderr)
        return 2

    # advise() resolves RELATIVE paths against its own DECK_DIR, which
    # would double the prefix for paths already inside it — hand it
    # absolutes only.
    deck_paths = [Path(d).resolve() for d in args.decks]
    missing = [p for p in deck_paths if not p.is_file()]
    if missing:
        print(f"error: no such deck(s): {missing}", file=sys.stderr)
        return 2
    # Stage INSIDE the Forge deck dir (the decks' own directory):
    # Forge resolves decks from its userdata tree, so a sibling
    # subfolder is invisible to it — the first pilot's pods exited in
    # ~20s with 0 games because the staged decks didn't exist as far
    # as Forge was concerned.
    stage_dir = (Path(args.stage_dir) if args.stage_dir
                 else deck_paths[0].parent)

    rows = []
    for p in deck_paths:
        print(f"[tier3] {p.name} ...", file=sys.stderr, flush=True)
        rows.append(run_deck(p, args.bracket, args.k, args.games,
                             stage_dir, dry_run=args.dry_run, arms=arms))

    wins_by_arm = {arm: sum(1 for r in rows if r.get("winner") == arm)
                   for arm in arms}
    summary = {
        "rows": rows,
        "decks": len(rows),
        "arms": list(arms),
        "wins_by_arm": wins_by_arm,
        # Kept for the readers already parsing the two-arm shape.
        "score_wins": wins_by_arm.get("score", 0),
        "bucket_wins": wins_by_arm.get("bucket", 0),
        "ties": sum(1 for r in rows if r.get("winner") == "tie"),
        "skipped": sum(1 for r in rows if r.get("skipped")),
    }
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2),
                                  encoding="utf-8")
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        for r in rows:
            if r.get("skipped"):
                print(f"  {r['deck']}: SKIPPED ({r['skipped']})")
                if r.get("arms_identical") is False and args.dry_run:
                    for arm in arms:
                        swaps = r.get(f"{arm}_arm") or {}
                        print(f"    {arm:>6} arm: +{swaps.get('adds')} "
                              f"-{swaps.get('cuts')}")
            else:
                margins = " vs ".join(
                    f"{arm} {r.get(f'{arm}_margin')}" for arm in arms)
                print(f"  {r['deck']}: {margins} -> {r.get('winner')}")
        tally = " / ".join(f"{arm} {wins_by_arm[arm]}" for arm in arms)
        print(f"{tally} / tie {summary['ties']} / "
              f"skipped {summary['skipped']} over {summary['decks']} decks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
