"""FP-015 pooled per-swap analysis — combine N gated result files.

The two-box pre-registration runs the per-swap harness
(``scripts/validate_card_score_perswap.py``) independently per arm and
pools the measured swaps for ONE gate decision. This script is that
pooling step, committed BLIND — written and merged before any real
result file was read — so the analysis cannot be shaped by the data.

WHAT IT DOES
------------
Inputs are two or more ``--out`` summary JSON files written by the
per-swap harness. From each file's ``rows`` it extracts every MEASURED
swap (per-swap CardScore + measured margin) via the harness's own
``_measured`` — so skipped swaps (no margin) and failed decks (no
``swaps`` at all) are excluded exactly as the single-run analysis
excludes them — and tags each swap with its source file.

POOLED STATISTICS
-----------------
Every statistic is IMPORTED from ``validate_card_score_perswap.py``,
never reimplemented: ``spearman_test`` (Spearman rho as Pearson over
mid-ranked average ranks — tie-correct — with the seeded one-sided
permutation p at 10,000 shuffles), ``group_contrast`` (top-vs-bottom
Welch t interval, conservative next-lower-df critical value), and
``gate_verdict`` (the verbatim verdict strings). Drift between the
single-run math and the pooled math would invalidate the
pre-registration; importing makes drift impossible.

GATE (pre-registered 2026-08-01, applied to the POOLED swaps)
-------------------------------------------------------------
CardScore is predictive iff pooled Spearman rho > 0 with one-sided
permutation p < .05 AND the pooled top-K group's mean margin exceeds
the bottom-K group's. Anything else: not predictive.

Multiple testing: the pooled gate is the ONE pre-registered test
family — a single conjunction, no correction needed (requiring both
criteria only makes it stricter). Per-arm breakdowns printed here are
exploratory and NOT multiplicity-corrected; an interesting-looking
single-arm number is not a finding.

INPUT GUARDS (added 2026-08-05 after a dry-run file was pooled as an
arm — see docs/future-plans.md, FP-015 FINAL correction): a file whose
top-level ``dry_run`` is true is refused outright, and a file
contributing zero measured swaps is refused unless
``--allow-empty-arm`` is passed (which prints a prominent caveat).

Usage::

    python scripts/pool_perswap_results.py ARM1.json ARM2.json [ARM3 ...]
        [--out pooled.json] [--json] [--allow-empty-arm]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

# The per-swap harness is a sibling script, not a package module — load
# it the same way its own tests do and reuse its shipped statistics
# (see the module docstring for why importing, not reimplementing, is
# the whole point).
_PS_SPEC = importlib.util.spec_from_file_location(
    "validate_card_score_perswap", Path(__file__).resolve().parent
    / "validate_card_score_perswap.py")
_ps = importlib.util.module_from_spec(_PS_SPEC)
_PS_SPEC.loader.exec_module(_ps)

GATE_POLICY = _ps.GATE_POLICY
MIN_SPEARMAN_N = _ps.MIN_SPEARMAN_N

POOLED_MULTIPLE_TESTING_NOTE = (
    "one pre-registered test family: the pooled gate is a single "
    "conjunction (Spearman AND group direction), which needs no "
    "correction — requiring both only makes it stricter. Per-arm "
    "breakdowns are exploratory and NOT multiplicity-corrected; do "
    "not promote any single-arm number to a claim.")


class ResultFileError(ValueError):
    """A result file is missing, unreadable, or not a per-swap summary."""


def load_arm(path: Path, allow_empty_arm: bool = False) -> dict:
    """One arm's counts + measured swaps, each tagged with its source.

    Measured/skipped/failed are derived from the rows with the SAME
    predicates the single-run summary uses (``_measured`` for measured;
    swap-level ``skipped`` markers; row-level ``failed`` / ``skipped``)
    rather than trusting the file's own aggregate fields — a truncated
    or hand-edited file cannot smuggle in counts its rows don't back.

    GUARDS (2026-08-05 incident: a collaborator's --dry-run file,
    written to a shared --out path, was pooled as if it were a
    completed arm — it contributed 0 measured swaps and the gate
    silently ran over the other arm alone):

    * a file whose top-level ``dry_run`` is true is REFUSED outright —
      no override exists, dry-run output is never arm data;
    * a file contributing ZERO measured swaps is refused unless
      ``allow_empty_arm`` — it may be unlabeled legacy dry-run output
      (files written before the ``dry_run`` field existed carry no
      label) or a genuinely empty arm, and only a human can tell.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResultFileError(f"{path}: cannot read ({exc})") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResultFileError(f"{path}: not valid JSON ({exc})") from exc
    if isinstance(data, dict) and data.get("dry_run") is True:
        raise ResultFileError(
            f"{path}: this file is DRY-RUN output (top-level "
            f"dry_run=true) — a dry run simulates no games and is NOT "
            f"a completed arm; it must never be pooled. Re-run the "
            f"per-swap harness without --dry-run to produce real arm "
            f"data. (There is no override for this refusal.)")
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ResultFileError(
            f"{path}: no 'rows' list — not a validate_card_score_perswap "
            f"--out summary")
    if not all(isinstance(r, dict)
               and all(isinstance(s, dict) for s in (r.get("swaps") or []))
               for r in rows):
        raise ResultFileError(
            f"{path}: malformed rows — every row (and every swap) must "
            f"be an object")
    # Tag key is "source_file", NOT "source" — the harness's swap dicts
    # already carry a "source" (the candidate's evidence source) and the
    # tag must not clobber it.
    measured = [{**s, "source_file": path.name}
                for s in _ps._measured(rows)]
    for s in measured:
        if not isinstance(s.get("card_score"), (int, float)) \
                or not isinstance(s.get("margin"), (int, float)):
            raise ResultFileError(
                f"{path}: measured swap {s.get('card')!r} has a "
                f"non-numeric card_score/margin")
    if not measured and not allow_empty_arm:
        raise ResultFileError(
            f"{path}: contributes ZERO measured swaps. Two "
            f"possibilities: (1) it is unlabeled legacy DRY-RUN output "
            f"(files written before the top-level dry_run field "
            f"existed carry no label), in which case it must not be "
            f"pooled — re-run the arm for real; or (2) it is a "
            f"genuinely empty arm (every deck/swap skipped or failed). "
            f"Pooling an empty arm silently evaluates the gate over "
            f"the other arm(s) alone. If you have verified it is a "
            f"real completed arm, re-run with --allow-empty-arm to "
            f"admit it (a caveat will be printed).")
    return {
        "file": path.name,
        "path": str(path),
        "decks": len(rows),
        "measured_swaps": len(measured),
        "skipped_swaps": sum(1 for r in rows
                             for s in (r.get("swaps") or [])
                             if s.get("skipped")),
        "skipped_decks": sum(1 for r in rows if r.get("skipped")),
        "failed_decks": sum(1 for r in rows if r.get("failed")),
        "_measured": measured,
    }


def build_pooled_summary(arms: list[dict]) -> dict:
    """Pool the arms' measured swaps and apply the pre-registered gate."""
    measured = [s for arm in arms for s in arm["_measured"]]
    scores = [s["card_score"] for s in measured]
    margins = [s["margin"] for s in measured]
    top = [s["margin"] for s in measured if s.get("group") == "top"]
    bottom = [s["margin"] for s in measured if s.get("group") == "bottom"]

    spearman = (_ps.spearman_test(scores, margins)
                if len(measured) >= MIN_SPEARMAN_N else None)
    contrast = _ps.group_contrast(top, bottom)
    gate = _ps.gate_verdict(spearman, contrast, len(measured))

    # Exploratory only (see POOLED_MULTIPLE_TESTING_NOTE): a per-arm
    # rho is a curiosity, never a criterion.
    per_arm_rho: dict[str, Optional[float]] = {}
    for arm in arms:
        if len(arm["_measured"]) >= MIN_SPEARMAN_N:
            per_arm_rho[arm["file"]] = _ps.spearman_rho(
                [s["card_score"] for s in arm["_measured"]],
                [s["margin"] for s in arm["_measured"]])

    return {
        "arms": [{k: v for k, v in arm.items() if k != "_measured"}
                 for arm in arms],
        "pooled_measured_swaps": len(measured),
        "swaps_by_group": {"top": len(top), "bottom": len(bottom)},
        "measured": measured,   # the full pooled table, deliberately
        "spearman": spearman,
        "group_contrast": contrast,
        "per_arm_spearman_exploratory": per_arm_rho,
        "gate": gate,
        "gate_policy": GATE_POLICY,
        "multiple_testing": POOLED_MULTIPLE_TESTING_NOTE,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FP-015 pooled per-swap analysis: apply the "
                    "pre-registered gate to the measured swaps pooled "
                    "across two or more per-swap result files.")
    parser.add_argument("results", nargs="+",
                        help="two or more validate_card_score_perswap "
                             "--out summary JSON files (one per arm)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--out", default=None,
                        help="also write the pooled summary JSON here")
    parser.add_argument("--allow-empty-arm", action="store_true",
                        help="admit a result file contributing zero "
                             "measured swaps (refused by default: an "
                             "empty arm may be unlabeled legacy "
                             "dry-run output, and pooling it silently "
                             "evaluates the gate over the other "
                             "arm(s) alone); a labeled dry-run file "
                             "is refused regardless")
    args = parser.parse_args(argv)

    if len(args.results) < 2:
        print("error: pooling needs at least 2 result files (the "
              "pre-registered decision is over the pooled arms)",
              file=sys.stderr)
        return 2
    try:
        arms = [load_arm(Path(p), allow_empty_arm=args.allow_empty_arm)
                for p in args.results]
    except ResultFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = build_pooled_summary(arms)
    empty_arms = [arm["file"] for arm in summary["arms"]
                  if arm["measured_swaps"] == 0]
    caveat = None
    if args.allow_empty_arm and empty_arms:
        caveat = ("CAVEAT: --allow-empty-arm admitted arm(s) with ZERO "
                  f"measured swaps: {', '.join(empty_arms)} — the "
                  "pooled gate is effectively evaluated over the "
                  "remaining arm(s) alone; make sure the empty file(s) "
                  "are genuinely empty arms and not unlabeled dry-run "
                  "output.")
        # Prominent in every mode: stderr always, plus a top-level
        # field so the --out / --json record carries it too.
        print(caveat, file=sys.stderr)
    summary["empty_arm_caveat"] = caveat
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2),
                                  encoding="utf-8")
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        for arm in summary["arms"]:
            print(f"  {arm['file']}: {arm['measured_swaps']} measured / "
                  f"{arm['skipped_swaps']} skipped swaps / "
                  f"{arm['skipped_decks']} skipped decks / "
                  f"{arm['failed_decks']} failed decks over "
                  f"{arm['decks']} decks")
        if summary["empty_arm_caveat"]:
            print(summary["empty_arm_caveat"])
        print(f"pooled n: {summary['pooled_measured_swaps']} measured "
              f"swaps (top {summary['swaps_by_group']['top']} / bottom "
              f"{summary['swaps_by_group']['bottom']})")
        sp = summary["spearman"]
        if sp:
            print(f"pooled spearman: rho {sp['rho']:+.3f}, "
                  f"p {sp['p_value']:.4f} over {sp['n']} swaps "
                  f"({sp['method']})")
        ct = summary["group_contrast"]
        if ct:
            ci_txt = (f" [{ct['ci_low']:+.3f}, {ct['ci_high']:+.3f}] "
                      f"(Welch df {ct['df']})"
                      if ct.get("ci_low") is not None else
                      " (interval needs >= 2 per group)")
            print(f"pooled top {ct['top_n']} mean {ct['top_mean']:+.3f} "
                  f"vs bottom {ct['bottom_n']} mean "
                  f"{ct['bottom_mean']:+.3f}; diff "
                  f"{ct['diff']:+.3f}{ci_txt}")
        for name, rho in summary["per_arm_spearman_exploratory"].items():
            rho_txt = f"{rho:+.3f}" if rho is not None else "undefined"
            print(f"  exploratory rho [{name}]: {rho_txt}")
        for name in ("spearman", "group_contrast", "overall"):
            print(f"gate[{name}]: {summary['gate'][name]}")
        print(f"gate policy: {summary['gate_policy']}")
        print(f"multiple testing: {summary['multiple_testing']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
