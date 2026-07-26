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
from collections import Counter
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


def _swap_signature(swaps: tuple[list[str], list[str]]) -> tuple:
    """Order-insensitive identity of one arm's swap set.

    Two arms that pick the SAME cards in a different order stage
    card-for-card identical decklists, so simming them measures the
    harness' noise floor, not ranking. Comparing the ordered lists let
    such a null replicate through as a real row: the 2026-07-25 tier-3
    pilot's Hash deck differed only in cut ORDER, and its two identical
    decks scored +0.130 and -0.217 — a 0.348 swing wider than every
    real effect the pilot measured (docs/future-plans.md, "Tier-3 pilot
    RESULT"). Multisets, not sets: a repeated card (basic lands) is a
    real difference in what gets staged.
    """
    adds, cuts = swaps
    return Counter(adds), Counter(cuts)


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


# --- (d) CI-based gating + (b) null-replicate noise floor -------------------
# Policy (set 2026-07-26): the CardScore/bubble flag earns default-on
# ONLY if the challenger arm's PAIRED per-deck margin advantage over the
# baseline arm has a 95% CI excluding zero across >= GATE_MIN_DECKS
# decks AND a mean advantage above the MEASURED null-replicate noise
# floor. A 2-of-3 winner tally (the pilot's headline) gates nothing.

GATE_MIN_DECKS = 6

#: Two-sided 95% t critical values by degrees of freedom; the sparse
#: tail uses the next-lower entry (conservative), >30 ~ normal.
_T_CRIT_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
              6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
              11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
              20: 2.086, 25: 2.060, 30: 2.042}


def _t_crit(df: int) -> float:
    if df <= 0:
        return float("inf")
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    lower = [k for k in _T_CRIT_95 if k <= df]
    if lower:
        return _T_CRIT_95[max(lower)]
    return 1.96 if df > 30 else 12.706


def paired_ci(diffs: list) -> Optional[dict]:
    """Mean of paired differences with a 95% t-interval (pure stdlib)."""
    n = len(diffs)
    if n < 2:
        return None
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = (var / n) ** 0.5
    half = _t_crit(n - 1) * se
    return {"n": n, "mean": mean, "se": se,
            "ci_low": mean - half, "ci_high": mean + half,
            "excludes_zero": (mean - half) > 0 or (mean + half) < 0}


def run_null_replicate(
    deck_path: Path,
    bracket: int,
    games: int,
    stage_dir: Path,
    compare_fn: Optional[Callable] = None,
) -> dict:
    """(b): sim an UNMODIFIED copy of the deck against itself.

    Any non-zero margin here is pure simulation noise at this games/pod
    setting — the published floor real arm differences must clear. The
    two copies get distinct staged names + Name= stamps (attribution
    invariant), and are removed afterwards like the real arms.
    """
    from commander_builder.dck_meta import rewrite_name
    original_text = deck_path.read_text(encoding="utf-8")
    stage_dir.mkdir(parents=True, exist_ok=True)
    stem = deck_path.stem
    a = stage_dir / f"{stem}__tier3_nullA.dck"
    b = stage_dir / f"{stem}__tier3_nullB.dck"
    a.write_text(rewrite_name(original_text, a.stem), encoding="utf-8")
    b.write_text(rewrite_name(original_text, b.stem), encoding="utf-8")
    if compare_fn is None:
        from commander_builder.compare_versions import compare as compare_fn
    try:
        report = compare_fn(old_deck=a.name, new_deck=b.name,
                            bracket=bracket, games_per_pod=games,
                            deck_dir=stage_dir)
    finally:
        for leftover in stage_dir.glob(f"{stem}__tier3_null*.dck"):
            try:
                leftover.unlink()
            except OSError:
                pass
    old_w, new_w = report.old_stats.wins, report.new_stats.wins
    decisive = old_w + new_w
    return {"deck": deck_path.name, "null": True,
            "games": report.old_stats.games,
            "margin": ((new_w - old_w) / decisive) if decisive else None}


def build_summary(rows: list, arms: tuple, null_rows: list) -> dict:
    """Aggregate rows into the CI-gated summary (pure; unit-tested)."""
    wins_by_arm = {arm: sum(1 for r in rows if r.get("winner") == arm)
                   for arm in arms}
    margins = {arm: [r[f"{arm}_margin"] for r in rows
                     if r.get(f"{arm}_margin") is not None]
               for arm in arms}
    null_margins = [r["margin"] for r in null_rows
                    if r.get("margin") is not None]
    noise_floor = None
    if null_margins:
        abs_m = [abs(m) for m in null_margins]
        noise_floor = {"n": len(abs_m),
                       "mean_abs_margin": sum(abs_m) / len(abs_m),
                       "max_abs_margin": max(abs_m)}
    baseline = arms[0]
    paired: dict = {}
    gate: dict = {}
    for arm in arms[1:]:
        diffs = [r[f"{arm}_margin"] - r[f"{baseline}_margin"]
                 for r in rows
                 if r.get(f"{arm}_margin") is not None
                 and r.get(f"{baseline}_margin") is not None]
        ci = paired_ci(diffs)
        paired[arm] = ci
        if ci is None or ci["n"] < GATE_MIN_DECKS:
            gate[arm] = (f"insufficient-n (need >= {GATE_MIN_DECKS} "
                         "paired decks)")
        elif not (ci["mean"] > 0 and ci["excludes_zero"]):
            gate[arm] = "fail (95% CI does not show a positive advantage)"
        elif noise_floor is None:
            gate[arm] = "insufficient-null-floor (run --null-replicates)"
        elif ci["mean"] <= noise_floor["mean_abs_margin"]:
            gate[arm] = "fail (advantage below the measured noise floor)"
        else:
            gate[arm] = "pass"
    return {
        "rows": rows,
        "null_rows": null_rows,
        "decks": len(rows),
        "arms": list(arms),
        "wins_by_arm": wins_by_arm,
        # Kept for the readers already parsing the two-arm shape.
        "score_wins": wins_by_arm.get("score", 0),
        "bucket_wins": wins_by_arm.get("bucket", 0),
        "ties": sum(1 for r in rows if r.get("winner") == "tie"),
        "skipped": sum(1 for r in rows if r.get("skipped")),
        "mean_margin_by_arm": {a: (sum(v) / len(v) if v else None)
                               for a, v in margins.items()},
        "paired_vs_" + baseline: paired,
        "noise_floor": noise_floor,
        "gate": gate,
        "gate_policy": (
            f"default-on requires >= {GATE_MIN_DECKS} paired decks, a 95% "
            "paired-CI excluding zero, AND a mean advantage above the "
            "measured null-replicate noise floor"),
    }


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
        "arms_identical": all(_swap_signature(s) == _swap_signature(swap_sets[0])
                              for s in swap_sets),
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
        row["skipped"] = ("arms identical (same cards, any order) — "
                          "no signal to measure")
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
    parser.add_argument("--null-replicates", type=int, default=0,
                        help="(b) also sim the first N decks unmodified "
                             "against themselves to publish a measured "
                             "noise floor (default %(default)s)")
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

    null_rows: list = []
    if args.null_replicates and not args.dry_run:
        for p in deck_paths[:args.null_replicates]:
            print(f"[tier3] null replicate: {p.name} ...",
                  file=sys.stderr, flush=True)
            null_rows.append(run_null_replicate(
                p, args.bracket, args.games, stage_dir))

    summary = build_summary(rows, arms, null_rows)
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
        if summary.get("noise_floor"):
            nf = summary["noise_floor"]
            print(f"noise floor ({nf['n']} null replicate(s)): "
                  f"mean |margin| {nf['mean_abs_margin']:.3f}, "
                  f"max {nf['max_abs_margin']:.3f}")
        for arm, verdict in (summary.get("gate") or {}).items():
            ci = summary[f"paired_vs_{arms[0]}"].get(arm)
            ci_txt = (f"mean {ci['mean']:+.3f} "
                      f"[{ci['ci_low']:+.3f}, {ci['ci_high']:+.3f}]"
                      if ci else "no paired data")
            print(f"gate[{arm} vs {arms[0]}]: {verdict} ({ci_txt})")
        tally = " / ".join(
            f"{arm} {summary['wins_by_arm'][arm]}" for arm in arms)
        print(f"{tally} / tie {summary['ties']} / "
              f"skipped {summary['skipped']} over {summary['decks']} decks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
