"""Paired curator evaluation harness (FP-013 gate condition b) — stub.

The FP-013 unblock condition is twofold (docs/fp013-scope.md): (a) the
live knowledge_log holds >= 1,000 high-confidence curator iterations
(track with ``commander-improve --health``), and (b) THIS script exists
and runs — a paired eval that scores a candidate proposer (eventually
the fine-tuned model) against the baseline (the current Claude curator)
on a held-out manifest set and reports the win-rate delta.

There is no fine-tuned model today, so ``main()`` is deliberately a
dry-run: it loads the holdout set and reports its size plus
"no candidate proposer configured". The ``evaluate()`` loop underneath
is real and tested — when a candidate exists, wire it in as a
``Proposer`` callable and plug ``sim_fn`` into
``forge_batch.run_ab_simulation`` (write the two proposals to temp
``.dck`` files, sim each against the case's base deck, return the win
counts).

Interfaces:

- ``Proposer``: ``Callable[[EvalCase], str]`` — given a held-out case
  (audit manifest + base deck snapshot), return the proposed ``.dck``
  text. The baseline wraps the current auto-curate pipeline; the
  candidate wraps the fine-tuned model.
- ``sim_fn``: ``Callable[[str, str], tuple[int, int, int]]`` — sim the
  base deck text vs a proposed deck text, return
  ``(wins_base, wins_proposed, games)``.

Pure stdlib, mirroring scripts/margin_analysis.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

# scripts/ is not a package; resolve the src tree the same way the
# other analysis scripts do when run from a checkout.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commander_builder.knowledge_log import (  # noqa: E402
    DEFAULT_DB_PATH,
    FP013_MIN_GAMES,
    _connect,
    init_db,
)

Proposer = Callable[["EvalCase"], str]
SimFn = Callable[[str, str], "tuple[int, int, int]"]


@dataclass
class EvalCase:
    """One held-out manifest: the question a proposer must answer."""
    iteration_id: int
    deck_id: str
    deck_name: str
    bracket: int
    audit_manifest: dict
    deck_snapshot: str


@dataclass
class CaseResult:
    """Paired outcome for one case: both proposals simmed against the
    same base deck."""
    iteration_id: int
    deck_id: str
    baseline_win_rate: float
    candidate_win_rate: float
    delta: float


def load_holdout(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    min_games: int = FP013_MIN_GAMES,
    limit: Optional[int] = None,
) -> list[EvalCase]:
    """Pull qualifying iterations as the held-out eval set.

    Same high-confidence filter as ``fp013_gate_progress`` (manifest +
    decided verdict + >=min_games sim), plus ``deck_snapshot`` present —
    a proposer can't re-answer a manifest without the deck state it was
    asked about. Newest first so a ``--limit`` samples the most recent
    (post-attribution-fix) rows.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, deck_id, deck_name, bracket, audit_manifest, "
            "sim_report, deck_snapshot FROM iterations "
            "WHERE audit_manifest IS NOT NULL "
            "AND verdict IN ('kept', 'reverted', 'neutral') "
            "AND sim_report IS NOT NULL "
            "AND deck_snapshot IS NOT NULL "
            "ORDER BY id DESC"
        ).fetchall()
    cases: list[EvalCase] = []
    for row in rows:
        try:
            report = json.loads(row["sim_report"])
            manifest = json.loads(row["audit_manifest"])
        except (TypeError, ValueError):
            continue
        if not isinstance(report, dict) or not isinstance(manifest, dict):
            continue
        games = report.get("games") or report.get("total_games") or 0
        if not isinstance(games, (int, float)) or games < min_games:
            continue
        cases.append(EvalCase(
            iteration_id=row["id"],
            deck_id=row["deck_id"],
            deck_name=row["deck_name"],
            bracket=row["bracket"],
            audit_manifest=manifest,
            deck_snapshot=row["deck_snapshot"],
        ))
        if limit is not None and len(cases) >= limit:
            break
    return cases


def evaluate(
    cases: list[EvalCase],
    *,
    baseline_proposer: Proposer,
    candidate_proposer: Proposer,
    sim_fn: SimFn,
) -> dict:
    """Run the paired eval: for each case, both proposers answer the
    same manifest and each proposal is simmed against the SAME base
    deck, so the per-case delta isolates the proposer as the only
    variable. Returns the aggregate + per-case breakdown."""
    results: list[CaseResult] = []
    for case in cases:
        baseline_deck = baseline_proposer(case)
        candidate_deck = candidate_proposer(case)
        _, wins_baseline, games_b = sim_fn(case.deck_snapshot, baseline_deck)
        _, wins_candidate, games_c = sim_fn(case.deck_snapshot, candidate_deck)
        baseline_wr = wins_baseline / games_b if games_b else 0.0
        candidate_wr = wins_candidate / games_c if games_c else 0.0
        results.append(CaseResult(
            iteration_id=case.iteration_id,
            deck_id=case.deck_id,
            baseline_win_rate=baseline_wr,
            candidate_win_rate=candidate_wr,
            delta=candidate_wr - baseline_wr,
        ))
    deltas = [r.delta for r in results]
    return {
        "n": len(results),
        "mean_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "candidate_better": sum(1 for d in deltas if d > 0),
        "baseline_better": sum(1 for d in deltas if d < 0),
        "ties": sum(1 for d in deltas if d == 0),
        "cases": [asdict(r) for r in results],
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="eval_curator",
        description=(
            "Paired curator eval (FP-013). Stub: reports the held-out "
            "manifest set; the candidate-model hook is not wired yet."
        ),
    )
    p.add_argument("--db-path", default=None,
                   help="knowledge_log SQLite path (default: the live log).")
    p.add_argument("--min-games", type=int, default=FP013_MIN_GAMES,
                   help=f"Min sim games for a row to qualify "
                        f"(default {FP013_MIN_GAMES}).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the holdout at the N most recent cases.")
    p.add_argument("--json", action="store_true",
                   help="Emit the report as JSON.")
    args = p.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else DEFAULT_DB_PATH
    cases = load_holdout(db_path, min_games=args.min_games, limit=args.limit)

    if args.json:
        print(json.dumps({
            "holdout_size": len(cases),
            "min_games": args.min_games,
            "db_path": str(db_path),
            "candidate_configured": False,
        }))
    else:
        print(f"Holdout: {len(cases)} high-confidence curator iterations "
              f"(>= {args.min_games}-game decided verdicts with manifest + "
              f"deck snapshot) in {db_path}")
        print("No candidate proposer configured yet -- this is the FP-013 "
              "eval interface stub. Wire a fine-tuned model in as a "
              "Proposer and rerun.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
