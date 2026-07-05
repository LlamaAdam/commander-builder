"""Tests for scripts/eval_curator.py -- the paired curator eval harness
(FP-013 gate condition b, built as a stub + interface per the
fp013-scope memo).

Pure-logic tests: holdout loading from a tmp knowledge_log and the
paired evaluation loop with injected proposers + sim function. No
Forge, no network, no model call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_curator as ec  # noqa: E402

from commander_builder.knowledge_log import (  # noqa: E402
    Iteration,
    init_db,
    record_iteration,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "kl.sqlite"
    init_db(p)
    return p


def _row(db, *, deck_id="d1", manifest={"added": ["A"], "removed": ["B"]},
         verdict="kept", sim_report={"games": 40}, snapshot="[Main]\n1 A\n"):
    return record_iteration(Iteration(
        deck_id=deck_id, deck_name=deck_id, bracket=3,
        audit_manifest=manifest, verdict=verdict,
        sim_report=sim_report, deck_snapshot=snapshot,
    ), db_path=db)


# --------------------------------------------------------------------------- #
# load_holdout
# --------------------------------------------------------------------------- #

def test_load_holdout_filters_high_confidence(db):
    """Only rows carrying the full (manifest, decided verdict,
    >=min_games sim, deck snapshot) triple qualify as eval cases."""
    good = _row(db, deck_id="good")
    _row(db, deck_id="pending", verdict="pending")
    _row(db, deck_id="lowgames", sim_report={"games": 5})
    _row(db, deck_id="nomanifest", manifest=None)
    _row(db, deck_id="nosnapshot", snapshot=None)

    cases = ec.load_holdout(db)
    assert [c.deck_id for c in cases] == ["good"]
    case = cases[0]
    assert case.iteration_id == good
    assert case.bracket == 3
    assert case.audit_manifest == {"added": ["A"], "removed": ["B"]}
    assert case.deck_snapshot == "[Main]\n1 A\n"


def test_load_holdout_respects_limit(db):
    for i in range(3):
        _row(db, deck_id=f"d{i}")
    assert len(ec.load_holdout(db)) == 3
    assert len(ec.load_holdout(db, limit=2)) == 2


# --------------------------------------------------------------------------- #
# evaluate — the paired loop
# --------------------------------------------------------------------------- #

def _case(deck_id="d1"):
    return ec.EvalCase(
        iteration_id=1, deck_id=deck_id, deck_name=deck_id, bracket=3,
        audit_manifest={"added": [], "removed": []},
        deck_snapshot="[Main]\n1 Base\n",
    )


def test_evaluate_reports_paired_win_rate_delta():
    """Each proposer's deck is simmed against the SAME base deck; the
    report carries the per-case candidate-minus-baseline delta and the
    aggregate."""
    def baseline_proposer(case):
        return "[Main]\n1 Baseline\n"

    def candidate_proposer(case):
        return "[Main]\n1 Candidate\n"

    def sim_fn(base_text, proposed_text):
        # (wins_base, wins_proposed, games): candidate wins 3-1,
        # baseline splits 2-2.
        if "Candidate" in proposed_text:
            return (1, 3, 4)
        return (2, 2, 4)

    report = ec.evaluate(
        [_case("d1"), _case("d2")],
        baseline_proposer=baseline_proposer,
        candidate_proposer=candidate_proposer,
        sim_fn=sim_fn,
    )
    assert report["n"] == 2
    assert report["candidate_better"] == 2
    assert report["baseline_better"] == 0
    assert report["ties"] == 0
    assert report["mean_delta"] == pytest.approx(0.25)
    case_report = report["cases"][0]
    assert case_report["baseline_win_rate"] == pytest.approx(0.5)
    assert case_report["candidate_win_rate"] == pytest.approx(0.75)
    assert case_report["delta"] == pytest.approx(0.25)


def test_evaluate_empty_cases():
    report = ec.evaluate(
        [], baseline_proposer=lambda c: "", candidate_proposer=lambda c: "",
        sim_fn=lambda a, b: (0, 0, 0),
    )
    assert report["n"] == 0
    assert report["mean_delta"] is None


# --------------------------------------------------------------------------- #
# main — stub behavior (no candidate model exists yet)
# --------------------------------------------------------------------------- #

def test_main_reports_holdout_and_not_configured(db, capsys):
    _row(db, deck_id="good")
    rc = ec.main(["--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1" in out
    assert "no candidate proposer" in out.lower()


def test_main_json(db, capsys):
    _row(db, deck_id="good")
    rc = ec.main(["--db-path", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["holdout_size"] == 1
    assert payload["candidate_configured"] is False
