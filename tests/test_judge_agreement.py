"""FP-016 Phase 2 stub — ``scripts/judge_agreement.py``.

The test that matters most here is the NO-DATA one. The flag is off by
default, so zero paired rows is the expected state on the day this ships,
and an empty table printed with tidy headers and 0% everywhere would read
as a result ("the instruments never agree") rather than as an absence
("nobody has measured anything"). The script must say the second thing.
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


def _row(db, *, sim=None, judge=None, report=None) -> int:
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    if sim is not None:
        update_iteration_sim(row_id, sim, sim_report={"wins_a": 1}, db_path=db)
    if judge is not None:
        update_iteration_judge(row_id, judge, report, db_path=db)
    return row_id


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


def test_g3_is_reported_as_not_computed_rather_than_passing(tmp_path, capsys):
    """A gate that silently reads as passed is worse than one that says it
    has not been run."""
    db = tmp_path / "kl.sqlite"
    _row(db, sim="kept", judge="kept", report={"order_flip": False})
    judge_agreement.main(["--db-path", str(db)])
    out = capsys.readouterr().out
    assert "G3 consensus bias    NOT COMPUTED" in out
    assert "staple-ward vs intent-ward" in out

    collected = judge_agreement.collect(db)
    assert judge_agreement.analyze(collected["paired"])["g3_computed"] is False


def test_thresholds_match_the_pre_registered_values():
    """Declared 2026-08-17, before any results existed. Changing these
    after data lands is moving the goalposts, so they are pinned."""
    assert judge_agreement.G1_ORDER_FLIP_MAX == 0.25
    assert judge_agreement.G2_KEPT_MAX == 0.80
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
