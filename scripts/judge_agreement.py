"""FP-016 Phase 2 (stub) — the instrument agreement table, and the kill gates.

WHAT THIS IS FOR
================
Phase 1 logs two verdicts per pairing from two instruments with different
blind spots: the Forge A/B sim (``verdict``) and the blinded LLM panel
(``judge_verdict``). This script is the deliverable that reads them side
by side — where they agree, where they diverge, and whether the judge has
already failed one of the kill criteria that were pre-registered BEFORE
any results existed (FP-016 §7).

WHAT IT IS NOT
==============
It is not a validation. Agreement between two instruments is not truth;
both can be wrong together (FP-016 §8). The strongest honest claim this
table can ever support is "two instruments with different blind spots
agree", and the script says so in its own output rather than leaving the
reader to remember it.

THE NO-DATA PATH IS A FEATURE
=============================
``COMMANDER_BUILDER_DECK_JUDGE`` is off by default, so the expected state
of this script on the day it ships is ZERO rows. An empty agreement table
printed with tidy headers and 0.0% everywhere looks exactly like a result
— it reads as "the instruments never agree" rather than "nobody has
measured anything". So the no-data path prints a plain statement of what
is missing and how to start collecting, and prints no table at all.

USAGE
=====
::

    python scripts/judge_agreement.py [--db-path knowledge_log.sqlite] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from commander_builder.deck_judge import (  # noqa: E402
    DECK_JUDGE_ENV_VAR,
    OPINION_CAVEAT,
)

#: The verdict labels both instruments draw from, in report order.
VERDICTS: tuple[str, ...] = ("kept", "reverted", "neutral", "inconclusive")

#: FP-016 §7, declared 2026-08-17 before any results existed. The sample
#: the gates are evaluated over.
KILL_CRITERIA_SAMPLE = 50

#: G1 — self-consistency. Order-swap flips the verdict on more than this
#: share of pairings => the judge disagrees with itself and cannot judge.
G1_ORDER_FLIP_MAX = 0.25

#: G2 — discrimination. It returns ``kept`` on more than this share =>
#: it is measuring agreeableness, not quality.
G2_KEPT_MAX = 0.80


def _pct(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def collect(db_path: Optional[Path] = None) -> dict:
    """Gather every row carrying BOTH verdicts.

    "Both" means a real sim verdict (not ``pending`` — an unfinished sim
    has nothing to agree or disagree with) AND a non-NULL
    ``judge_verdict``. Rows with only one are counted separately so the
    reader can see WHY the joinable population is small, which on day one
    is the whole story.
    """
    from commander_builder.knowledge_log import all_iterations

    rows = all_iterations(db_path=db_path)
    paired: list[dict] = []
    judged_only = 0
    simmed_only = 0
    for it in rows:
        has_sim = bool(it.verdict) and it.verdict != "pending"
        has_judge = bool(it.judge_verdict)
        if has_sim and has_judge:
            report = it.judge_report or {}
            paired.append({
                "id": it.id,
                "deck_id": it.deck_id,
                "sim_verdict": it.verdict,
                "judge_verdict": it.judge_verdict,
                "order_flip": bool(report.get("order_flip")),
                "discarded": int(report.get("discarded") or 0),
            })
        elif has_judge:
            judged_only += 1
        elif has_sim:
            simmed_only += 1
    return {
        "rows_total": len(rows),
        "paired": paired,
        "judged_only": judged_only,
        "simmed_only": simmed_only,
    }


def analyze(paired: list) -> dict:
    """Agreement counts + the G1/G2 tallies, from the paired rows alone."""
    n = len(paired)
    matrix: Counter = Counter(
        (row["sim_verdict"], row["judge_verdict"]) for row in paired
    )
    agreements = sum(
        count for (sim, judge), count in matrix.items() if sim == judge
    )
    order_flips = sum(1 for row in paired if row["order_flip"])
    judge_kept = sum(1 for row in paired if row["judge_verdict"] == "kept")
    return {
        "n": n,
        "matrix": {f"{sim}|{judge}": count for (sim, judge), count in matrix.items()},
        "agreements": agreements,
        "agreement_rate": _pct(agreements, n),
        "g1_order_flips": order_flips,
        "g1_order_flip_rate": _pct(order_flips, n),
        "g1_threshold": G1_ORDER_FLIP_MAX,
        "g1_failed": _pct(order_flips, n) > G1_ORDER_FLIP_MAX,
        "g2_kept": judge_kept,
        "g2_kept_rate": _pct(judge_kept, n),
        "g2_threshold": G2_KEPT_MAX,
        "g2_failed": _pct(judge_kept, n) > G2_KEPT_MAX,
        # G3 (consensus bias) needs swaps LABELED staple-ward vs
        # intent-ward, which nothing in the log records yet. Reported as
        # not-computed rather than defaulted to "passing": a gate that
        # silently reads as passed is worse than one that says it has not
        # been run.
        "g3_computed": False,
        "sample_target": KILL_CRITERIA_SAMPLE,
        "sample_reached": n >= KILL_CRITERIA_SAMPLE,
    }


def _render_no_data(collected: dict) -> str:
    """The honest empty state. No table — see the module docstring."""
    lines = [
        "FP-016 Phase 2 — instrument agreement: NO DATA YET.",
        "",
        f"  rows in the knowledge log:        {collected['rows_total']}",
        f"  rows with a sim verdict only:     {collected['simmed_only']}",
        f"  rows with a judge verdict only:   {collected['judged_only']}",
        "  rows carrying BOTH verdicts:      0",
        "",
        "There is nothing to compare, so no table is printed. An empty",
        "agreement table would read as 'the instruments never agree'",
        "rather than 'nobody has measured anything yet'.",
        "",
        "To start collecting paired verdicts, run the improve loop with",
        f"the judge enabled:  {DECK_JUDGE_ENV_VAR}=1",
        "",
        "Phase 1 is observe-only: the judge writes its opinion beside the",
        "sim verdict and never changes what the loop does. The kill",
        f"criteria are evaluated over the first {KILL_CRITERIA_SAMPLE} pairings.",
        "",
        f"  {OPINION_CAVEAT}",
    ]
    return "\n".join(lines)


def _render(collected: dict, stats: dict) -> str:
    n = stats["n"]
    lines = [
        "FP-016 Phase 2 — instrument agreement (sim verdict vs judge opinion)",
        "",
        f"  paired rows: {n}"
        + ("" if stats["sample_reached"]
           else f"   (kill criteria are declared over "
                f"{KILL_CRITERIA_SAMPLE}; this is a partial read)"),
        f"  not joinable: {collected['simmed_only']} sim-only, "
        f"{collected['judged_only']} judge-only "
        f"(of {collected['rows_total']} rows in the log)",
        f"  agree on the same label: {stats['agreements']}/{n}"
        f"  ({stats['agreement_rate']:.0%})",
        "",
        "  Agreement table — rows: sim verdict, columns: judge opinion",
    ]
    header = "    " + "sim \\ judge".ljust(14) + "".join(
        v.ljust(14) for v in VERDICTS
    )
    lines.append(header)
    for sim in VERDICTS:
        cells = "".join(
            str(stats["matrix"].get(f"{sim}|{judge}", 0)).ljust(14)
            for judge in VERDICTS
        )
        lines.append("    " + sim.ljust(14) + cells)
    lines += [
        "",
        "  Pre-registered kill criteria (FP-016 §7, declared 2026-08-17):",
        f"    G1 self-consistency  order-flip {stats['g1_order_flips']}/{n} "
        f"({stats['g1_order_flip_rate']:.0%}) vs >{G1_ORDER_FLIP_MAX:.0%} "
        f"=> {'FAILED' if stats['g1_failed'] else 'passing'}",
        f"    G2 discrimination    kept {stats['g2_kept']}/{n} "
        f"({stats['g2_kept_rate']:.0%}) vs >{G2_KEPT_MAX:.0%} "
        f"=> {'FAILED' if stats['g2_failed'] else 'passing'}",
        "    G3 consensus bias    NOT COMPUTED — needs swaps labeled "
        "staple-ward vs intent-ward, which the log does not record yet.",
        "",
        "  Agreement is not truth: both instruments can be wrong together.",
        "  This table is informative, never confirmatory. Do not write it",
        "  up as 'validated'.",
        "",
        f"  {OPINION_CAVEAT}",
    ]
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="judge_agreement",
        description=(
            "FP-016 Phase 2: the sim-vs-judge agreement table and the "
            "pre-registered kill-criteria tallies. Says so plainly when "
            "there is no data yet."
        ),
    )
    parser.add_argument(
        "--db-path", type=Path, default=None,
        help="knowledge log to read (default: the configured one)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    collected = collect(args.db_path)
    paired = collected["paired"]
    if not paired:
        if args.as_json:
            print(json.dumps({
                "status": "no_data",
                "reason": "no rows carry both a sim verdict and a judge verdict",
                **{k: v for k, v in collected.items() if k != "paired"},
                "caveat": OPINION_CAVEAT,
            }, indent=2))
        else:
            print(_render_no_data(collected))
        # Exit 0: "nothing measured yet" is the expected state while the
        # flag is off, not a failure of this script.
        return 0

    stats = analyze(paired)
    if args.as_json:
        print(json.dumps({
            "status": "ok",
            **{k: v for k, v in collected.items() if k != "paired"},
            "pairings": paired,
            "stats": stats,
            "caveat": OPINION_CAVEAT,
        }, indent=2))
    else:
        print(_render(collected, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
