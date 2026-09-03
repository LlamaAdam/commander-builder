"""FP-016 Phase 2 stub — ``scripts/judge_agreement.py``.

The test that matters most here is the NO-DATA one. The flag is off by
default, so zero paired rows is the expected state on the day this ships,
and an empty table printed with tidy headers and 0% everywhere would read
as a result ("the instruments never agree") rather than as an absence
("nobody has measured anything"). The script must say the second thing.

The G3 tests carry the same burden one level down. G3 became computable on
2026-08-27, when ``deck_judge`` started labeling each pairing's swap
direction; what is pinned here is that it still refuses to report a number
it has not earned — an unlabeled pairing is outside its population, a thin
arm is NOT COMPUTED rather than "passing", and the exact rule is printed on
every path including the no-data one.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from commander_builder.knowledge_log import (
    Iteration, record_iteration, update_iteration_judge, update_iteration_sim,
)

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "judge_agreement", REPO / "scripts" / "judge_agreement.py",
)
judge_agreement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(judge_agreement)


def _row(db, *, sim=None, judge=None, report=None, direction=None) -> int:
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    if sim is not None:
        update_iteration_sim(row_id, sim, sim_report={"wins_a": 1}, db_path=db)
    if judge is not None:
        report = dict(report or {})
        if direction is not None:
            report["swap_direction"] = direction
        update_iteration_judge(row_id, judge, report, db_path=db)
    return row_id


def _arms(db, *, staple, staple_kept, intent, intent_kept):
    """Populate both G3 arms with a given size and approval count."""
    for i in range(staple):
        _row(db, sim="kept", judge="kept" if i < staple_kept else "reverted",
             report={"order_flip": False}, direction="staple_ward")
    for i in range(intent):
        _row(db, sim="kept", judge="kept" if i < intent_kept else "reverted",
             report={"order_flip": False}, direction="intent_ward")


# --------------------------------------------------------------------------- #
# The no-data path
# --------------------------------------------------------------------------- #

def test_empty_log_says_so_and_prints_no_table(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    rc = judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out

    assert rc == 0                       # absence is not a failure
    assert "NO DATA YET" in out
    assert "rows carrying BOTH verdicts:      0" in out
    # No table at all — not an empty one.
    assert "sim \\ judge" not in out
    assert "Agreement table" not in out
    # And it says how to start collecting.
    assert "COMMANDER_BUILDER_DECK_JUDGE=1" in out


def test_sim_only_rows_still_count_as_no_data(tmp_path, capsys):
    """The common real case: the improve loop has been running for weeks
    with the judge flag off. Those rows are not half an agreement table."""
    db = tmp_path / "kl.sqlite"
    _row(db, sim="kept")
    _row(db, sim="neutral")
    rc = judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "NO DATA YET" in out
    assert "rows with a sim verdict only:     2" in out
    assert "Agreement table" not in out


def test_pending_sim_rows_are_not_pairable(tmp_path, capsys):
    """An unfinished sim has nothing to agree or disagree with."""
    db = tmp_path / "kl.sqlite"
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    update_iteration_judge(row_id, "kept", {"order_flip": False}, db_path=db)
    rc = judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 0 and "NO DATA YET" in out
    assert "rows with a judge verdict only:   1" in out


def test_no_data_json_mode_is_explicit_about_it(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    assert judge_agreement.main(["--db-path", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_data"
    assert "both a sim verdict and a judge verdict" in payload["reason"]
    assert "OPINION panel" in payload["caveat"]


# --------------------------------------------------------------------------- #
# The populated path
# --------------------------------------------------------------------------- #

def test_agreement_table_counts_the_joined_rows(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    clean = {"order_flip": False, "discarded": 0}
    _row(db, sim="kept", judge="kept", report=clean)
    _row(db, sim="kept", judge="neutral", report=clean)
    _row(db, sim="neutral", judge="neutral", report=clean)
    _row(db, sim="kept")                       # sim only — not joinable

    rc = judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "paired rows: 3" in out
    assert "agree on the same label: 2/3" in out
    assert "sim \\ judge" in out
    # Partial read is labeled as such — 3 is not 50.
    assert "partial read" in out
    # And the standing honesty note is not optional.
    assert "Agreement is not truth" in out
    assert "OPINION panel" in out


def test_g1_and_g2_are_tallied_against_the_declared_thresholds(tmp_path):
    db = tmp_path / "kl.sqlite"
    flipped = {"order_flip": True, "discarded": 0}
    clean = {"order_flip": False, "discarded": 0}
    _row(db, sim="kept", judge="inconclusive", report=flipped)
    _row(db, sim="kept", judge="kept", report=clean)
    _row(db, sim="kept", judge="kept", report=clean)
    _row(db, sim="kept", judge="kept", report=clean)

    collected = judge_agreement.collect(db)
    stats = judge_agreement.analyze(collected["paired"])
    assert stats["n"] == 4
    assert stats["g1_order_flips"] == 1
    assert stats["g1_order_flip_rate"] == 0.25
    assert stats["g1_failed"] is False        # >25%, not >=
    assert stats["g2_kept"] == 3
    assert stats["g2_kept_rate"] == 0.75
    assert stats["g2_failed"] is False


def test_gates_fail_loudly_when_crossed(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    flipped = {"order_flip": True, "discarded": 0}
    for _ in range(3):
        _row(db, sim="kept", judge="kept", report=flipped)
    _row(db, sim="kept", judge="kept", report={"order_flip": False})

    judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert "G1 self-consistency" in out and "FAILED" in out
    assert "G2 discrimination" in out


# --------------------------------------------------------------------------- #
# G3 — consensus bias (computable since 2026-08-27)
# --------------------------------------------------------------------------- #

def test_g3_is_not_computed_when_no_pairing_is_labeled(tmp_path, capsys):
    """A gate that silently reads as passed is worse than one that says it
    has not been run. An unlabeled row is outside G3's population, not in
    one of its arms."""
    db = tmp_path / "kl.sqlite"
    _row(db, sim="kept", judge="kept", report={"order_flip": False})
    judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert "G3 consensus bias    NOT COMPUTED" in out
    assert "staple-ward 0/10, intent-ward 0/10" in out

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_computed"] is False
    assert stats["g3_failed"] is False
    assert stats["g3_excess"] is None
    assert stats["g3_labeled"]["unknown"] == 1


def test_g3_prints_its_exact_rule_on_every_path(tmp_path, capsys):
    """The rule is a judgement call — §7 names the comparison but no
    statistic — so it is printed whether or not it fired. Including on the
    no-data path, where G3 is now waiting for DATA rather than for an
    implementation."""
    db = tmp_path / "kl.sqlite"
    judge_agreement.main(["--db-path", str(db)])          # no data at all
    assert judge_agreement.G3_RULE in capsys.readouterr().out

    _row(db, sim="kept", judge="kept", report={"order_flip": False})
    judge_agreement.main(["--db-path", str(db)])          # unlabeled row
    assert judge_agreement.G3_RULE in capsys.readouterr().out

    _arms(db, staple=10, staple_kept=5, intent=10, intent_kept=5)
    judge_agreement.main(["--db-path", str(db)])          # computed
    assert judge_agreement.G3_RULE in capsys.readouterr().out


def test_g3_needs_both_arms_before_it_reads_a_difference(tmp_path):
    """One full arm and one thin one is not a comparison: two arms of 3 can
    differ by 33 points on a single row changing its mind."""
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=12, staple_kept=12, intent=2, intent_kept=0)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_computed"] is False
    assert stats["g3_failed"] is False        # NOT "passing", and not failed
    assert "intent-ward" in stats["g3_reason"]
    # The useful half of a NOT COMPUTED result is still reported.
    assert stats["g3_arms"]["staple_ward"]["n"] == 12
    assert stats["g3_arms"]["staple_ward"]["approval_rate"] == 1.0
    assert stats["g3_arms"]["intent_ward"]["n"] == 2


def test_g3_fails_when_staple_ward_approval_runs_away(tmp_path, capsys):
    """The failure this gate exists for: the judge endorses generic-staple
    swaps far more readily than deck-specific ones."""
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=9, intent=10, intent_kept=3)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_computed"] is True
    assert stats["g3_excess"] == pytest.approx(0.6)
    assert stats["g3_failed"] is True

    judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert "G3 consensus bias    FAILED" in out
    assert "+60%" in out


def test_g3_passes_when_the_two_arms_track_each_other(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=6, intent=10, intent_kept=5)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_computed"] is True
    assert stats["g3_excess"] == pytest.approx(0.1)
    assert stats["g3_failed"] is False

    judge_agreement.main(["--db-path", str(db)])
    assert "G3 consensus bias    passing" in capsys.readouterr().out


def test_g3_is_a_difference_not_a_level(tmp_path):
    """A judge that approves nearly everything trips G2, not G3. G3 only
    fires when the two arms come APART — otherwise a run of genuinely good
    staple-ward swaps would be indicted as bias."""
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=10, intent=10, intent_kept=10)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_excess"] == 0.0
    assert stats["g3_failed"] is False
    assert stats["g2_failed"] is True        # caught by the right gate


def test_g3_boundary_is_strictly_greater_than(tmp_path):
    """Exactly at the threshold is passing, matching G1/G2's ">"."""
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=7, intent=10, intent_kept=5)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_excess"] == pytest.approx(judge_agreement.G3_STAPLE_EXCESS_MAX)
    assert stats["g3_failed"] is False


def test_g3_ignores_mixed_neither_and_unrecognized_directions(tmp_path):
    """Only the two named arms are evidence. ``mixed`` / ``neither`` are
    real labels but not part of the comparison; a value from a future build
    must fall OUT of the population rather than into an arm."""
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=10, intent=10, intent_kept=10)
    _row(db, sim="kept", judge="kept", report={"order_flip": False},
         direction="mixed")
    _row(db, sim="kept", judge="kept", report={"order_flip": False},
         direction="neither")
    _row(db, sim="kept", judge="kept", report={"order_flip": False},
         direction="something_new")

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["g3_arms"]["staple_ward"]["n"] == 10
    assert stats["g3_arms"]["intent_ward"]["n"] == 10
    assert stats["g3_labeled"]["mixed"] == 1
    assert stats["g3_labeled"]["neither"] == 1
    # The unrecognized label is counted as unknown, not as a fifth arm.
    assert stats["g3_labeled"]["unknown"] == 1
    assert "something_new" not in stats["g3_labeled"]


def test_g3_json_mode_carries_the_arms_and_the_rule(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    _arms(db, staple=10, staple_kept=9, intent=10, intent_kept=3)
    assert judge_agreement.main(["--db-path", str(db), "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)["stats"]
    assert stats["g3_failed"] is True
    assert stats["g3_rule"] == judge_agreement.G3_RULE
    assert stats["g3_arms"]["staple_ward"]["kept"] == 9


def test_thresholds_match_the_pre_registered_values():
    """G1/G2 declared 2026-08-17 and G3 on 2026-08-27 — all of them before
    any results existed (the flag is off by default and the log holds zero
    paired rows). Changing these after data lands is moving the goalposts,
    so they are pinned."""
    assert judge_agreement.G1_ORDER_FLIP_MAX == 0.25
    assert judge_agreement.G2_KEPT_MAX == 0.80
    assert judge_agreement.G3_STAPLE_EXCESS_MAX == 0.20
    assert judge_agreement.G3_MIN_PER_ARM == 10
    assert judge_agreement.KILL_CRITERIA_SAMPLE == 50


def test_json_mode_carries_the_per_pairing_rows(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    _row(db, sim="reverted", judge="kept", report={"order_flip": False})
    assert judge_agreement.main(["--db-path", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["pairings"][0]["sim_verdict"] == "reverted"
    assert payload["pairings"][0]["judge_verdict"] == "kept"
    assert payload["stats"]["agreements"] == 0


# --- R3 C-13 (2026-09-03): inconclusive == inconclusive is not agreement ---

def test_mutual_inconclusive_is_not_counted_as_agreement(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    clean = {"order_flip": False, "discarded": 0}
    _row(db, sim="inconclusive", judge="inconclusive", report=clean)
    _row(db, sim="kept", judge="kept", report=clean)
    _row(db, sim="inconclusive", judge="kept", report=clean)

    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["n"] == 3
    assert stats["decided"] == 1
    assert stats["undecided"] == 2
    assert stats["both_inconclusive"] == 1
    assert stats["agreements"] == 1
    assert stats["agreement_rate"] == 1.0          # 1/1 decided, not 2/3

    judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert "agree on the same label: 1/1" in out
    assert "both inconclusive: 1 (not counted as agreement)" in out


def test_agreement_rate_over_no_decided_pairs_is_zero_not_a_crash(tmp_path):
    db = tmp_path / "kl.sqlite"
    _row(db, sim="inconclusive", judge="inconclusive",
         report={"order_flip": False, "discarded": 0})
    stats = judge_agreement.analyze(judge_agreement.collect(db)["paired"])
    assert stats["decided"] == 0 and stats["agreements"] == 0
    assert stats["agreement_rate"] == 0.0
