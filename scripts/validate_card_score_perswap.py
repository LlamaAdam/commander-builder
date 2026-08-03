"""FP-015 per-swap validation — does CardScore predict SINGLE-swap value?

Two whole-ordering tier-3 gated runs FAILED their gate (mean bubble
advantage +0.075 [-0.159, +0.310] at n=6, then +0.088 [-0.125, +0.302]
at n=9 — see docs/future-plans.md, the 2026-07-28 and 2026-07-31
"Tier-3 GATED RESULT" sections). The 2026-07-31 conclusion names the
only reopening path: **per-swap A/B at scale, which measures individual
swaps instead of whole-ordering bundles.** This harness is that design.

WHAT IT MEASURES
----------------
Per deck: run the advisor's candidate-add generation ONCE (the real
production ``advise()`` path, flag untouched), score EVERY candidate
add with ``CardScore`` through the flag-independent internals
(``card_score.deck_context`` + ``score_card`` — the same seam
``bubble_analysis.score_deck`` consumes; ``COMMANDER_BUILDER_CARD_SCORE``
is never flipped, because the question is whether the NUMBER predicts
swap value, not whether the flag-on ranking pipeline works). Select the
top-K and bottom-K candidates by score, stage each as a SINGLE-swap
deck (that one add + the advisor's paired cut, applied through the
shared ``_apply_swaps_to_dck`` legality path), and A/B sim each staged
deck against the unmodified base at equal game counts.

THE PAIRED CUT IS HELD FIXED per deck: every staged swap uses the
advisor's top-ranked matchable cut. A per-swap-varying cut would
confound "was this add good" with "was that cut good"; holding the cut
constant makes within-deck margin differences attributable to the add —
the thing CardScore ranks. The chosen cut is recorded per row.

If CardScore is a real ranking prior, high-scored swaps should measure
better margins than low-scored swaps. Unlike the whole-ordering design
this does not require the two arms to disagree (the identical-arms skip
ate 10 of 19 decks in the 2026-07-31 run, including every EDHREC-average
premade); every deck with candidates and one matchable cut contributes
2K per-swap observations.

GATE POLICY (pre-registered 2026-08-01, BEFORE any gated run)
-------------------------------------------------------------
CardScore is predictive iff BOTH hold over the pooled measured swaps:

1. Spearman rank correlation between CardScore and measured per-swap
   margin has rho > 0 with one-sided permutation p < .05, AND
2. the top-K group's mean margin exceeds the bottom-K group's.

Anything else: not predictive; FP-015 stays concluded. The null-noise
reference (``--null-replicates``, same semantics as tier-3) is
published context, NOT a third criterion.

Statistics, stated up front:

* Spearman rho is Pearson over average ranks (pure stdlib, tied ranks
  handled by mid-ranking). Its p-value is a seeded one-sided
  PERMUTATION test (10,000 shuffles, +1 smoothing) — chosen over a
  t-approximation because the pooled n is small, margins are coarse
  (few decisive games per pod) so ties are expected, and a permutation
  test is exact-in-distribution under those conditions where the
  t-approximation is not.
* The top-vs-bottom contrast carries a T-BASED (Welch) 95% interval —
  chosen over a permutation interval because with K=3 per deck the
  group relabeling space is too coarse to support interval endpoints,
  while Welch handles unequal group variances and the conservative
  next-lower-df critical value keeps the interval honest at tiny n.
  The GATE criterion is only the direction of the mean difference; the
  interval is printed so the reader sees how little that direction may
  mean.
* Multiple testing: the gate is ONE pre-registered conjunction (both
  criteria must hold, which only makes the test stricter). Every other
  number in the summary — per-deck rhos, the contrast interval, the
  noise reference — is exploratory and NOT multiplicity-corrected; a
  "significant"-looking slice below the gate line is not a finding.

Usage::

    python scripts/validate_card_score_perswap.py DECK.dck [DECK2 ...]
        [--bracket 3] [--top-k 3] [--bottom-k 3] [--games 40]
        [--null-replicates 0] [--out summary.json] [--dry-run] [--json]

Everything Forge-facing reuses the tier-3 harness machinery
(``scripts/validate_card_score.py``): the ``compare_versions.compare``
seam, staging in the REAL deck dir, ``Name=`` restamping to the staged
filename stem (log-parser attribution invariant), try/finally staged-
deck cleanup including mid-sim crashes, staged-text degeneracy skips,
per-deck failure containment with partial-result summaries, and the
null-replicate noise reference.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# The tier-3 harness is a sibling script, not a package module — load it
# the same way its own tests do and reuse its shipped machinery instead
# of re-implementing staging/cleanup/noise-reference logic.
_T3_SPEC = importlib.util.spec_from_file_location(
    "validate_card_score", Path(__file__).resolve().parent
    / "validate_card_score.py")
_t3 = importlib.util.module_from_spec(_T3_SPEC)
_T3_SPEC.loader.exec_module(_t3)

GATE_MIN_NULL_REPLICATES = _t3.GATE_MIN_NULL_REPLICATES

#: Below this many pooled measured swaps the Spearman criterion is
#: reported as not evaluated (rho over 1-2 points is not a correlation).
#: Small-but-legal n needs no extra guard: the permutation p-value
#: self-penalizes (n=3 cannot reach p < .05).
MIN_SPEARMAN_N = 3

#: Permutation count + seed for the Spearman p-value. Seeded so two
#: runs over the same result file print the same p (and so the tests
#: can pin values).
SPEARMAN_PERMUTATIONS = 10_000
SPEARMAN_SEED = 20260801

GATE_POLICY = (
    "pre-registered 2026-08-01, before any gated run: CardScore is "
    "predictive iff (1) pooled Spearman rho between CardScore and "
    "measured per-swap margin is > 0 with one-sided permutation "
    "p < .05, AND (2) the top-K group's mean margin exceeds the "
    "bottom-K group's. Anything else: not predictive, FP-015 stays "
    "concluded. The null-replicate noise reference is published "
    "context, not a criterion.")

MULTIPLE_TESTING_NOTE = (
    "one pre-registered test family: the gate is a single conjunction "
    "(Spearman AND group direction), which needs no correction — "
    "requiring both only makes it stricter. Per-deck rhos, the contrast "
    "interval and the noise reference are exploratory and NOT "
    "multiplicity-corrected; do not promote any of them to a claim.")


# ---------------------------------------------------------------------------
# Pure-stdlib statistics (unit-tested on hand-checked fixtures)
# ---------------------------------------------------------------------------

def average_ranks(values: list) -> list[float]:
    """1-based ranks with ties mid-ranked (the Spearman convention)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and (values[order[j + 1]]
                                      == values[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rho(xs: list, ys: list) -> Optional[float]:
    """Spearman rho = Pearson correlation over average ranks.

    Computed as Pearson-over-ranks rather than the ``6*sum(d^2)``
    shortcut because the shortcut formula is WRONG under ties and
    per-swap margins tie constantly (few decisive games per pod).
    Returns None when undefined (n < 2, or either side constant).
    """
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def spearman_test(
    xs: list, ys: list,
    *,
    permutations: int = SPEARMAN_PERMUTATIONS,
    seed: int = SPEARMAN_SEED,
) -> Optional[dict]:
    """Rho plus a one-sided (greater) permutation p-value.

    ``p = (#{perm rho >= observed} + 1) / (permutations + 1)`` — the
    +1 smoothing keeps p > 0 (a Monte Carlo p of exactly zero is a
    lie). Seeded, so deterministic. One-sided because the gate claim
    is directional: CardScore should predict BETTER swaps.
    """
    rho = spearman_rho(xs, ys)
    if rho is None:
        return None
    rng = random.Random(seed)
    shuffled = list(ys)
    hits = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        r = spearman_rho(xs, shuffled)
        if r is not None and r >= rho - 1e-12:
            hits += 1
    return {"rho": rho, "n": len(xs),
            "p_value": (hits + 1) / (permutations + 1),
            "method": (f"one-sided permutation test, "
                       f"{permutations} shuffles, seed {seed}")}


def group_contrast(top: list, bottom: list) -> Optional[dict]:
    """Top-group vs bottom-group mean margin, with a Welch t interval.

    The interval is T-BASED (Welch, conservative next-lower-df critical
    value from the tier-3 table) — see the module docstring for why a
    permutation interval is the wrong tool at K=3 per group. The GATE
    reads only ``diff > 0``; the interval is printed context. Returns
    None when either group is empty; the interval fields are None when
    either group has fewer than 2 measurements (a variance needs 2).
    """
    if not top or not bottom:
        return None
    n1, n2 = len(top), len(bottom)
    m1, m2 = sum(top) / n1, sum(bottom) / n2
    out: dict = {"top_n": n1, "bottom_n": n2,
                 "top_mean": m1, "bottom_mean": m2, "diff": m1 - m2,
                 "ci_low": None, "ci_high": None, "df": None,
                 "method": ("Welch t-based 95% interval, conservative "
                            "next-lower-df critical value; gate reads "
                            "only the sign of diff")}
    if n1 < 2 or n2 < 2:
        return out
    v1 = sum((x - m1) ** 2 for x in top) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in bottom) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        # Zero variance in both groups: the interval is the point.
        out.update(ci_low=out["diff"], ci_high=out["diff"],
                   df=n1 + n2 - 2)
        return out
    df = ((v1 / n1 + v2 / n2) ** 2
          / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)))
    half = _t3._t_crit(int(df)) * se
    out.update(ci_low=out["diff"] - half, ci_high=out["diff"] + half,
               df=round(df, 2))
    return out


# ---------------------------------------------------------------------------
# Candidate scoring + selection
# ---------------------------------------------------------------------------

def _default_score_fn(deck_path: Path, bracket: int) -> Callable:
    """CardScore via the FLAG-INDEPENDENT internals.

    Builds one ``card_score.DeckContext`` from the real deck text
    (quantities matter — the PR #37 lesson) and scores each candidate
    with the evidence numbers the advisor already attached
    (``inclusion_pct`` / ``synergy_pct`` / ``role``), exactly like the
    flag-on ranking seam does. ``COMMANDER_BUILDER_CARD_SCORE`` is
    never read or written: the internals are called directly.
    """
    from commander_builder import card_score
    ctx = card_score.deck_context(
        deck_text=deck_path.read_text(encoding="utf-8"), bracket=bracket)

    def score_fn(rec) -> float:
        ev = getattr(rec, "evidence", None) or {}
        return card_score.score_card(
            rec.card, ctx,
            inclusion_pct=ev.get("inclusion_pct"),
            synergy_pct=ev.get("synergy_pct"),
            role=ev.get("role"),
        ).total
    return score_fn


def rank_candidates(add_recs: list, score_fn: Callable) -> tuple[list[dict],
                                                                 list[str]]:
    """Score every candidate add; return ``(ranked, unscored)``.

    ``ranked`` is the FULL descending list (recorded whole in the
    output so a future re-analysis can re-bucket without re-running
    the advisor), tie-broken by name so two runs rank identically.
    ``score_card`` never raises by contract, but an injected scorer
    might — a failed candidate is excluded from selection and reported
    under ``unscored``, never silently scored 0 ("we couldn't score
    this" and "this scored terribly" are opposite conclusions).
    """
    ranked: list[dict] = []
    unscored: list[str] = []
    for rec in add_recs:
        ev = getattr(rec, "evidence", None) or {}
        try:
            score = float(score_fn(rec))
        except Exception:  # noqa: BLE001 — one bad candidate != no run
            unscored.append(rec.card)
            continue
        ranked.append({"card": rec.card, "card_score": score,
                       "source": ev.get("source"), "role": ev.get("role")})
    ranked.sort(key=lambda r: (-r["card_score"], r["card"].lower()))
    return ranked, unscored


def select_swaps(ranked: list[dict], top_k: int,
                 bottom_k: int) -> list[dict]:
    """Top-K and bottom-K by CardScore, disjoint by construction.

    The bottom group is drawn from the candidates left AFTER the top
    group, so a short candidate list shrinks the bottom group rather
    than double-counting a card into both.
    """
    top = ranked[:max(0, top_k)]
    rest = ranked[len(top):]
    bottom = rest[max(0, len(rest) - bottom_k):] if bottom_k > 0 else []
    return ([{**r, "group": "top"} for r in top]
            + [{**r, "group": "bottom"} for r in bottom])


def _pick_paired_cut(original_text: str, cuts: list[str]) -> Optional[str]:
    """The advisor's top-ranked cut that actually matches a [Main] line.

    Held FIXED across every staged swap for the deck (see the module
    docstring for why). Advisor cuts occasionally miss the decklist
    (DFC naming drift, an LLM backend hallucination); walking to the
    first matchable one per-deck beats losing the whole deck to a
    doomed pairing.
    """
    from commander_builder.web.deck_text_ops import _dck_name_key
    main_keys = {key for (section, key), _qty
                 in _t3._staged_signature(original_text)
                 if section == "[main]"}
    for cut in cuts:
        if _dck_name_key(cut) in main_keys:
            return cut
    return None


# ---------------------------------------------------------------------------
# Per-deck driver
# ---------------------------------------------------------------------------

def run_deck(
    deck_path: Path,
    bracket: int,
    top_k: int,
    bottom_k: int,
    games: int,
    stage_dir: Path,
    dry_run: bool = False,
    advise_fn: Optional[Callable] = None,
    score_fn: Optional[Callable] = None,
    compare_fn: Optional[Callable] = None,
) -> dict:
    """One deck's per-swap rows: advise once, score all, sim 2K swaps."""
    if advise_fn is None:
        from commander_builder.improvement_advisor import advise as advise_fn
    original_text = deck_path.read_text(encoding="utf-8")
    report = advise_fn(deck_path, bracket)
    recs = list(getattr(report, "recommendations", None) or [])
    add_recs = [r for r in recs if r.action == "add"]
    cut_names = [r.card for r in recs if r.action == "cut"]

    row: dict = {"deck": deck_path.name, "bracket": bracket,
                 "top_k": top_k, "bottom_k": bottom_k,
                 "candidates": len(add_recs)}
    if not add_recs:
        row["skipped"] = "advisor produced no candidate adds"
        return row

    if score_fn is None:
        score_fn = _default_score_fn(deck_path, bracket)
    ranked, unscored = rank_candidates(add_recs, score_fn)
    row["ranked"] = ranked          # the FULL list, deliberately
    row["unscored"] = unscored
    if not ranked:
        row["skipped"] = "no candidate could be scored"
        return row

    paired_cut = _pick_paired_cut(original_text, cut_names)
    row["paired_cut"] = paired_cut
    if paired_cut is None:
        row["skipped"] = ("advisor proposed no cut matching the "
                          "decklist — cannot stage a size-legal "
                          "single swap")
        return row

    base_signature = _t3._staged_signature(original_text)
    swaps = select_swaps(ranked, top_k, bottom_k)
    row["swaps"] = swaps
    for swap in swaps:
        # ONE add + the fixed paired cut, through the shared legality
        # path — the same staging the advisor's apply path uses.
        proposed, applied = _t3._stage_preview(
            original_text, [swap["card"]], [paired_cut])
        if (not applied
                or _t3._staged_signature(proposed) == base_signature):
            swap["skipped"] = ("degenerate stage — staged text is "
                              "identical to the base deck")
            continue
        swap["_proposed"] = proposed
    if dry_run:
        for swap in swaps:
            swap.pop("_proposed", None)
            swap.setdefault("skipped", "dry run")
        row["skipped"] = "dry run"
        return row

    stage_dir.mkdir(parents=True, exist_ok=True)
    stem = deck_path.stem
    if compare_fn is None:
        from commander_builder.compare_versions import compare as compare_fn
    from commander_builder.dck_meta import rewrite_name
    staged_paths: list[Path] = []
    try:
        # Base staged ONCE per deck. Name= MUST match the staged
        # filename stem — log_parser attributes wins by Forge's
        # displayed deck name (the pre-e8777b6 attribution bug parsed
        # 0 games when base and arm shared a Name=).
        base_copy = stage_dir / f"{stem}__perswap_base.dck"
        staged_paths.append(base_copy)
        base_copy.write_text(rewrite_name(original_text, base_copy.stem),
                             encoding="utf-8")
        for i, swap in enumerate(swaps):
            proposed = swap.pop("_proposed", None)
            if proposed is None:
                continue
            staged = stage_dir / (
                f"{stem}__perswap_{i:02d}_{swap['group']}.dck")
            staged_paths.append(staged)
            staged.write_text(rewrite_name(proposed, staged.stem),
                              encoding="utf-8")
            sim = compare_fn(
                old_deck=base_copy.name,
                new_deck=staged.name,
                bracket=bracket,
                games_per_pod=games,
                deck_dir=stage_dir,
            )
            old_wins, new_wins = sim.old_stats.wins, sim.new_stats.wins
            decisive = old_wins + new_wins
            swap["sim"] = {"old_wins": old_wins, "new_wins": new_wins,
                           "games": sim.old_stats.games}
            swap["margin"] = ((new_wins - old_wins) / decisive
                              if decisive else None)
    finally:
        # Staged decks live in the REAL deck dir (Forge requirement) —
        # remove them so they never pollute the deck list / web UI,
        # INCLUDING when a sim crashes mid-swap. The persisted compare
        # reports remain the durable record. Unlink the EXACT paths
        # recorded at staging time — never a glob built from the stem:
        # real stems contain [USER]/[B3] and pathlib.glob treats square
        # brackets as character classes, matching nothing (staged decks
        # then leak permanently into the live deck dir).
        for leftover in staged_paths:
            try:
                leftover.unlink()
            except OSError:
                pass
    return row


# ---------------------------------------------------------------------------
# Summary + gate
# ---------------------------------------------------------------------------

def _measured(rows: list) -> list[dict]:
    """Every simmed swap with a real margin, tagged with its deck."""
    out: list[dict] = []
    for r in rows:
        for swap in r.get("swaps") or []:
            if swap.get("margin") is not None:
                out.append({"deck": r.get("deck"), **swap})
    return out


def build_summary(rows: list, null_rows: list) -> dict:
    """Aggregate per-swap rows into the gated summary (pure; tested)."""
    measured = _measured(rows)
    scores = [s["card_score"] for s in measured]
    margins = [s["margin"] for s in measured]
    top = [s["margin"] for s in measured if s.get("group") == "top"]
    bottom = [s["margin"] for s in measured if s.get("group") == "bottom"]

    spearman = (spearman_test(scores, margins)
                if len(measured) >= MIN_SPEARMAN_N else None)
    contrast = group_contrast(top, bottom)

    # Exploratory only (see MULTIPLE_TESTING_NOTE): per-deck rho over
    # a handful of swaps is a curiosity, never a criterion.
    per_deck_rho: dict[str, Optional[float]] = {}
    for r in rows:
        deck_swaps = [s for s in measured if s["deck"] == r.get("deck")]
        if len(deck_swaps) >= MIN_SPEARMAN_N:
            per_deck_rho[r["deck"]] = spearman_rho(
                [s["card_score"] for s in deck_swaps],
                [s["margin"] for s in deck_swaps])

    null_margins = [n["margin"] for n in null_rows
                    if n.get("margin") is not None]
    noise_floor = None
    if null_margins:
        abs_m = [abs(m) for m in null_margins]
        noise_floor = {
            "n": len(abs_m),
            "mean_abs_margin": sum(abs_m) / len(abs_m),
            "max_abs_margin": max(abs_m),
            "sufficient": len(abs_m) >= GATE_MIN_NULL_REPLICATES,
            "note": ("published context only — the per-swap gate has "
                     "exactly two pre-registered criteria and this is "
                     "not one of them; it says how large a single "
                     "base-vs-self margin runs at this games/pod "
                     "setting"),
        }

    failed_decks = [{"deck": r.get("deck"), "error": r["failed"]}
                    for r in rows if r.get("failed")]
    failed_nulls = [{"deck": r.get("deck"), "error": r["failed"]}
                    for r in null_rows if r.get("failed")]

    gate: dict[str, str] = {}
    if spearman is None:
        gate["spearman"] = (
            f"not evaluated (need >= {MIN_SPEARMAN_N} measured swaps "
            f"with non-constant scores and margins; have "
            f"{len(measured)})")
    elif spearman["rho"] > 0 and spearman["p_value"] < 0.05:
        gate["spearman"] = (f"pass (rho {spearman['rho']:+.3f}, "
                            f"p {spearman['p_value']:.4f})")
    else:
        gate["spearman"] = (
            f"fail (rho {spearman['rho']:+.3f}, "
            f"p {spearman['p_value']:.4f} — needs rho > 0 and p < .05)")
    if contrast is None:
        gate["group_contrast"] = ("not evaluated (need >= 1 measured "
                                  "swap in each of top and bottom)")
    elif contrast["diff"] > 0:
        gate["group_contrast"] = (
            f"pass (top mean {contrast['top_mean']:+.3f} > bottom mean "
            f"{contrast['bottom_mean']:+.3f})")
    else:
        gate["group_contrast"] = (
            f"fail (top mean {contrast['top_mean']:+.3f} <= bottom "
            f"mean {contrast['bottom_mean']:+.3f})")
    if all(v.startswith("pass") for v in gate.values()):
        gate["overall"] = "pass — CardScore is predictive per policy"
    elif any(v.startswith("not evaluated") for v in gate.values()):
        gate["overall"] = "not evaluated (a criterion lacks data)"
    else:
        gate["overall"] = "fail — CardScore is not shown predictive"

    return {
        "rows": rows,
        "null_rows": null_rows,
        "decks": len(rows),
        "skipped_decks": sum(1 for r in rows if r.get("skipped")),
        "failed": len(failed_decks),
        "failed_decks": failed_decks,
        "failed_null_replicates": failed_nulls,
        "failure_note": (
            (f"{len(failed_decks)} deck(s) failed mid-run; a failed "
             "deck contributes no swaps and is excluded from every "
             "statistic — the gate below is over the surviving swaps "
             "only") if failed_decks else None),
        "measured_swaps": len(measured),
        "swaps_by_group": {"top": len(top), "bottom": len(bottom)},
        "skipped_swaps": sum(
            1 for r in rows for s in (r.get("swaps") or [])
            if s.get("skipped")),
        "spearman": spearman,
        "per_deck_spearman_exploratory": per_deck_rho,
        "group_contrast": contrast,
        "noise_floor": noise_floor,
        "gate": gate,
        "gate_policy": GATE_POLICY,
        "multiple_testing": MULTIPLE_TESTING_NOTE,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FP-015 per-swap validation: does CardScore predict "
                    "the measured margin of individual swaps?")
    parser.add_argument("decks", nargs="+", help=".dck paths")
    parser.add_argument("--bracket", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=3,
                        help="highest-scored candidates to sim "
                             "(default %(default)s)")
    parser.add_argument("--bottom-k", type=int, default=3,
                        help="lowest-scored candidates to sim "
                             "(default %(default)s)")
    parser.add_argument("--games", type=int, default=40,
                        help="games per pod per swap (default %(default)s)")
    parser.add_argument("--stage-dir", default=None,
                        help="where staged decks go (default: the first "
                             "deck's own directory — Forge resolves decks "
                             "from its userdata tree, a sibling subfolder "
                             "is invisible to it)")
    parser.add_argument("--null-replicates", type=int, default=0,
                        help="also sim the first N decks unmodified "
                             "against themselves (same semantics as "
                             "tier-3) to publish a noise reference "
                             "(default %(default)s)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", default=None,
                        help="also write the summary JSON to this file "
                             "(clean — compare()'s progress lines go to "
                             "stdout, so redirecting stdout is NOT a "
                             "reliable way to capture the JSON)")
    args = parser.parse_args(argv)

    if args.top_k < 1 or args.bottom_k < 1:
        print("error: --top-k and --bottom-k must each be >= 1 (the "
              "gate contrasts the two groups)", file=sys.stderr)
        return 2
    deck_paths = [Path(d).resolve() for d in args.decks]
    missing = [p for p in deck_paths if not p.is_file()]
    if missing:
        print(f"error: no such deck(s): {missing}", file=sys.stderr)
        return 2
    stage_dir = (Path(args.stage_dir) if args.stage_dir
                 else deck_paths[0].parent)

    # Same containment contract as tier-3: one crashed deck (Forge
    # dying mid-pod, a timeout, a bad .dck) must not vaporize the run —
    # record it as a failed row, keep going, and always write --out.
    rows: list = []
    for p in deck_paths:
        print(f"[perswap] {p.name} ...", file=sys.stderr, flush=True)
        try:
            rows.append(run_deck(p, args.bracket, args.top_k,
                                 args.bottom_k, args.games, stage_dir,
                                 dry_run=args.dry_run))
        except Exception as exc:  # noqa: BLE001
            print(f"[perswap] {p.name} FAILED: {exc}",
                  file=sys.stderr, flush=True)
            rows.append({"deck": p.name,
                         "failed": f"{type(exc).__name__}: {exc}"})

    null_rows: list = []
    if args.null_replicates and not args.dry_run:
        for p in deck_paths[:args.null_replicates]:
            print(f"[perswap] null replicate: {p.name} ...",
                  file=sys.stderr, flush=True)
            try:
                null_rows.append(_t3.run_null_replicate(
                    p, args.bracket, args.games, stage_dir))
            except Exception as exc:  # noqa: BLE001
                print(f"[perswap] null replicate {p.name} FAILED: {exc}",
                      file=sys.stderr, flush=True)
                null_rows.append({"deck": p.name, "null": True,
                                  "failed":
                                  f"{type(exc).__name__}: {exc}"})

    summary = build_summary(rows, null_rows)
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2),
                                  encoding="utf-8")
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        for r in rows:
            if r.get("failed"):
                print(f"  {r['deck']}: FAILED ({r['failed']})")
                continue
            if r.get("skipped"):
                print(f"  {r['deck']}: SKIPPED ({r['skipped']})")
            for swap in r.get("swaps") or []:
                margin = swap.get("margin")
                status = (f"margin {margin:+.3f}" if margin is not None
                          else f"skipped ({swap.get('skipped')})")
                print(f"    [{swap['group']:>6}] {swap['card']} "
                      f"(score {swap['card_score']:.1f}, cut "
                      f"{r.get('paired_cut')}): {status}")
        if summary.get("failure_note"):
            print(summary["failure_note"])
        sp = summary.get("spearman")
        if sp:
            print(f"spearman: rho {sp['rho']:+.3f}, "
                  f"p {sp['p_value']:.4f} over {sp['n']} swaps "
                  f"({sp['method']})")
        ct = summary.get("group_contrast")
        if ct:
            ci_txt = (f" [{ct['ci_low']:+.3f}, {ct['ci_high']:+.3f}] "
                      f"(Welch df {ct['df']})"
                      if ct.get("ci_low") is not None else
                      " (interval needs >= 2 per group)")
            print(f"top {ct['top_n']} mean {ct['top_mean']:+.3f} vs "
                  f"bottom {ct['bottom_n']} mean "
                  f"{ct['bottom_mean']:+.3f}; diff "
                  f"{ct['diff']:+.3f}{ci_txt}")
        nf = summary.get("noise_floor")
        if nf:
            print(f"null-noise reference ({nf['n']} replicate(s)): "
                  f"mean |margin| {nf['mean_abs_margin']:.3f}, "
                  f"max {nf['max_abs_margin']:.3f}"
                  + ("" if nf["sufficient"] else
                     f" — fewer than {GATE_MIN_NULL_REPLICATES}, "
                     "treat as anecdote"))
        for name in ("spearman", "group_contrast", "overall"):
            print(f"gate[{name}]: {summary['gate'][name]}")
        print(f"gate policy: {summary['gate_policy']}")
        print(f"multiple testing: {summary['multiple_testing']}")
        print(f"{summary['measured_swaps']} measured swaps "
              f"(top {summary['swaps_by_group']['top']} / bottom "
              f"{summary['swaps_by_group']['bottom']}) / "
              f"{summary['skipped_swaps']} skipped swaps / "
              f"{summary['skipped_decks']} skipped decks / "
              f"{summary['failed']} failed over {summary['decks']} decks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
