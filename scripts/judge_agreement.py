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

THE THREE GATES
===============
G1 (self-consistency) and G2 (discrimination) are per-pairing tallies and
have been computable since this script shipped. G3 (consensus bias) is
not: it compares the judge's approval rate across two POPULATIONS of
pairings — staple-ward and intent-ward — and until 2026-08-27 nothing
labeled a pairing as either, so it printed NOT COMPUTED. ``deck_judge``
now records a swap direction per pairing, and the exact rule this script
reads it with (a statistic §7 names in words but not in arithmetic) is
derived in the G3 block below and PRINTED on every run, including the
no-data one — where G3 is now waiting for data rather than for an
implementation.

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

from commander_builder._deck_judge_prompt import (  # noqa: E402
    SWAP_DIRECTIONS,
)
from commander_builder.deck_judge import (  # noqa: E402
    DECK_JUDGE_ENV_VAR,
    OPINION_CAVEAT,
)

#: The verdict labels both instruments draw from, in report order.
VERDICTS: tuple[str, ...] = ("kept", "reverted", "neutral", "inconclusive")

#: The labels that carry a reading. ``inconclusive`` means "this
#: instrument could not read the pairing" — a sim with too few decisive
#: games, a panel that split — and two of them are not an agreement
#: (2026-09-03, R3 C-13): mutual silence for unrelated reasons said
#: nothing about whether the instruments see the same thing.
DECIDED_VERDICTS: tuple[str, ...] = ("kept", "reverted", "neutral")

#: FP-016 §7, declared 2026-08-17 before any results existed. The sample
#: the gates are evaluated over.
KILL_CRITERIA_SAMPLE = 50

#: G1 — self-consistency. Order-swap flips the verdict on more than this
#: share of pairings => the judge disagrees with itself and cannot judge.
G1_ORDER_FLIP_MAX = 0.25

#: G2 — discrimination. It returns ``kept`` on more than this share =>
#: it is measuring agreeableness, not quality.
G2_KEPT_MAX = 0.80

# --- G3 — consensus bias ---------------------------------------------------
#
# FP-016 §7 states G3 in words, not in arithmetic: "Its preferences track
# 'cards with high EDHREC inclusion' more strongly than deck-specific fit,
# tested by scoring swaps that are staple-ward vs. intent-ward." It names
# the comparison and the two populations but no statistic and no number,
# because when it was written nothing produced the labels.
#
# THE CLOSEST HONEST COMPUTABLE VERSION, and why it is this one:
#
#   approval(arm) = P(judge_verdict == "kept" | swap_direction == arm)
#   G3 statistic  = approval(staple_ward) - approval(intent_ward)
#   G3 FAILS when the statistic exceeds G3_STAPLE_EXCESS_MAX.
#
# ``kept`` means the panel preferred deck B — the deck holding the ADDED
# cards — so approval(arm) is literally "how often the judge endorsed a
# swap of this kind". The DIFFERENCE, not the level, is the statistic:
# §7's claim is comparative ("more strongly than deck-specific fit"), and
# an absolute staple-ward approval rate cannot separate a consensus-chasing
# judge from a judge facing a run of genuinely good staple-ward swaps. The
# intent-ward arm is the control, and it is a control the judge itself
# produced under the same prompt, the same panel geometry and the same
# blinding.
#
# What this version is NOT, said plainly: it is not a measurement of
# EDHREC inclusion%. We have no inclusion% offline. ``staple_ward`` is
# membership in two shipped lists — ``staples.UNIVERSAL_STAPLES_LC``
# ("well over 50% of all decks") and the Commander Brackets Game Changers
# list — which is a coarse proxy for "generically included". A judge could
# fail this gate while being fine about inclusion% in general, or pass it
# while chasing consensus on cards neither list names. G3 stays an alarm,
# not a verdict, and the printed rule says so.

#: G3 threshold. Staple-ward approval may exceed intent-ward approval by
#: at most this much.
#:
#: PRE-REGISTERED 2026-08-27 — later than G1/G2's 2026-08-17, but still
#: before any results exist: the judge flag is default-off and the
#: knowledge log holds zero paired rows, so this number cannot have been
#: fitted to an outcome. 20 points is the same order as the slack G1 and
#: G2 allow, and is deliberately generous: staple-ward swaps ARE often
#: genuinely good, so a small positive gap is expected and is not the
#: failure mode. Changing this once pairings land is moving the
#: goalposts; a test pins it.
G3_STAPLE_EXCESS_MAX = 0.20

#: Minimum labeled pairings in EACH arm before the difference is read at
#: all. Two arms of 3 can differ by 33 points on one row changing its
#: mind. Below this the gate reports NOT COMPUTED and names which arm is
#: short — never "passing", because a gate that reads as passed on no
#: evidence is worse than one that says it has not been run.
G3_MIN_PER_ARM = 10


def _pct(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def _direction_of(report: dict) -> str:
    """The pairing's swap direction, or ``"unknown"``.

    Defensive on purpose. ``judge_report`` is a JSON blob that predates
    this field, may have been written by an older build, and may carry a
    value from a future one. Anything not in ``SWAP_DIRECTIONS`` reads as
    ``unknown`` — an unrecognized label must fall out of G3's population,
    never quietly into one of its arms.
    """
    value = report.get("swap_direction")
    return value if value in SWAP_DIRECTIONS else "unknown"


#: The one-line statement of G3's rule, printed on EVERY path — passing,
#: failing, not-computed and no-data alike. A gate whose definition is
#: only visible when it fires is a gate the reader has to take on trust.
G3_RULE = (
    f"P(judge says 'kept' | staple-ward swap) - "
    f"P(judge says 'kept' | intent-ward swap) > "
    f"{G3_STAPLE_EXCESS_MAX:.0%}, "
    f"read only once each arm has >= {G3_MIN_PER_ARM} labeled pairings"
)


def _g3(paired: list) -> dict:
    """The consensus-bias gate. See the G3 block above for the derivation.

    Always returns the arm counts and approval rates it managed to
    compute, even when it declines to read the difference — "staple-ward
    12/40, intent-ward 3/4" is the useful half of a NOT COMPUTED result,
    and hiding it would leave the reader unable to tell "nobody has run
    the judge" from "everything so far has been staple-ward".
    """
    by_direction: Counter = Counter(row["swap_direction"] for row in paired)
    arms: dict = {}
    for arm in ("staple_ward", "intent_ward"):
        rows = [r for r in paired if r["swap_direction"] == arm]
        kept = sum(1 for r in rows if r["judge_verdict"] == "kept")
        arms[arm] = {
            "n": len(rows),
            "kept": kept,
            # None, not 0.0: an arm with no rows has no approval rate, and
            # 0.0 would read as "the judge approved none of them".
            "approval_rate": (kept / len(rows)) if rows else None,
        }

    result = {
        "g3_rule": G3_RULE,
        "g3_threshold": G3_STAPLE_EXCESS_MAX,
        "g3_min_per_arm": G3_MIN_PER_ARM,
        "g3_labeled": {d: by_direction.get(d, 0) for d in SWAP_DIRECTIONS},
        "g3_arms": arms,
        "g3_excess": None,
        "g3_computed": False,
        "g3_failed": False,
        "g3_reason": "",
    }

    short = [
        arm for arm in ("staple_ward", "intent_ward")
        if arms[arm]["n"] < G3_MIN_PER_ARM
    ]
    if short:
        result["g3_reason"] = (
            "not enough labeled pairings in "
            + " and ".join(a.replace("_", "-") for a in short)
            + " ("
            + ", ".join(
                f"{a.replace('_', '-')} {arms[a]['n']}/{G3_MIN_PER_ARM}"
                for a in ("staple_ward", "intent_ward")
            )
            + ")"
        )
        return result

    excess = arms["staple_ward"]["approval_rate"] - arms["intent_ward"]["approval_rate"]
    result["g3_excess"] = excess
    result["g3_computed"] = True
    result["g3_failed"] = excess > G3_STAPLE_EXCESS_MAX
    result["g3_reason"] = (
        f"staple-ward approval {arms['staple_ward']['approval_rate']:.0%} "
        f"({arms['staple_ward']['kept']}/{arms['staple_ward']['n']}) minus "
        f"intent-ward {arms['intent_ward']['approval_rate']:.0%} "
        f"({arms['intent_ward']['kept']}/{arms['intent_ward']['n']}) "
        f"= {excess:+.0%}"
    )
    return result


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
                # Absent on every row written before 2026-08-27, and on
                # any row whose labeling failed. ``unknown`` (not a
                # guess, not None) so those rows land outside G3's
                # population instead of inside one of its arms.
                "swap_direction": _direction_of(report),
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
    """Agreement counts + the G1/G2 tallies, from the paired rows alone.

    ``agreements`` / ``agreement_rate`` are computed over ``decided``
    pairings only — rows where BOTH instruments returned a label in
    ``DECIDED_VERDICTS``. Pairings where either side is ``inconclusive``
    are reported separately (``undecided``, and ``both_inconclusive``
    for the mutual-silence case) and never counted as agreement
    (2026-09-03, R3 C-13; the headline used to count
    ``inconclusive == inconclusive`` as the instruments agreeing).
    """
    n = len(paired)
    matrix: Counter = Counter(
        (row["sim_verdict"], row["judge_verdict"]) for row in paired
    )
    decided_rows = [
        row for row in paired
        if row["sim_verdict"] in DECIDED_VERDICTS
        and row["judge_verdict"] in DECIDED_VERDICTS
    ]
    decided = len(decided_rows)
    agreements = sum(
        1 for row in decided_rows if row["sim_verdict"] == row["judge_verdict"]
    )
    both_inconclusive = sum(
        1 for row in paired
        if row["sim_verdict"] == "inconclusive"
        and row["judge_verdict"] == "inconclusive"
    )
    order_flips = sum(1 for row in paired if row["order_flip"])
    judge_kept = sum(1 for row in paired if row["judge_verdict"] == "kept")
    return {
        "n": n,
        "matrix": {f"{sim}|{judge}": count for (sim, judge), count in matrix.items()},
        "decided": decided,
        "undecided": n - decided,
        "both_inconclusive": both_inconclusive,
        "agreements": agreements,
        "agreement_rate": _pct(agreements, decided),
        "g1_order_flips": order_flips,
        "g1_order_flip_rate": _pct(order_flips, n),
        "g1_threshold": G1_ORDER_FLIP_MAX,
        "g1_failed": _pct(order_flips, n) > G1_ORDER_FLIP_MAX,
        "g2_kept": judge_kept,
        "g2_kept_rate": _pct(judge_kept, n),
        "g2_threshold": G2_KEPT_MAX,
        "g2_failed": _pct(judge_kept, n) > G2_KEPT_MAX,
        # G3 (consensus bias). Computable since 2026-08-27, when
        # ``deck_judge`` started recording each pairing's swap direction;
        # still reports NOT COMPUTED — never "passing" — until both arms
        # carry enough labeled pairings to read a difference from.
        **_g3(paired),
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
        # G3 became computable on 2026-08-27, when deck_judge started
        # labeling each pairing's swap direction. With zero pairings that
        # changes nothing about the answer — it is still NOT COMPUTED —
        # but it changes WHY, and the difference is the whole point: G3 is
        # now waiting for data rather than waiting for an implementation.
        "  G3 consensus bias    NOT COMPUTED — no pairings are labeled yet",
        f"                       (needs >= {G3_MIN_PER_ARM} in each arm).",
        f"                       rule: {G3_RULE}",
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
        f"  agree on the same label: {stats['agreements']}/{stats['decided']}"
        f"  ({stats['agreement_rate']:.0%}) over pairings both instruments "
        f"decided",
        f"  undecided (either side inconclusive): {stats['undecided']}"
        f"  — of which both inconclusive: {stats['both_inconclusive']} "
        f"(not counted as agreement)",
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
    ]
    # G3 prints its RULE on every path, computed or not: the definition is
    # a judgement call (see the G3 block at the top of this file) and a
    # reader who cannot see it cannot audit the number under it.
    g3_state = (
        "FAILED" if stats["g3_failed"]
        else "passing" if stats["g3_computed"]
        else "NOT COMPUTED"
    )
    lines.append(f"    G3 consensus bias    {g3_state} — {stats['g3_reason']}")
    lines.append(f"                         rule: {stats['g3_rule']}")
    labeled = stats["g3_labeled"]
    lines.append(
        "                         swap directions: "
        + ", ".join(f"{d.replace('_', '-')} {labeled.get(d, 0)}"
                    for d in SWAP_DIRECTIONS)
    )
    lines.append(
        "                         (staple-ward is membership in the "
        "universal-staples + Game Changers lists, a coarse proxy for "
        "EDHREC inclusion% — G3 is an alarm, not a measurement of it.)"
    )
    lines += [
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
