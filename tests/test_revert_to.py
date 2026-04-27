"""revert_to tests with isolated knowledge_log DBs."""
from pathlib import Path

import pytest

from commander_builder.knowledge_log import (
    Iteration,
    iterations_for_deck,
    record_iteration,
)
from commander_builder.revert_to import (
    revert_deck_to_version,
    revert_to_iteration,
)


_SNAPSHOT_V1 = "\n".join([
    "[metadata]",
    "Name=Test Deck",
    "Moxfield=stable-id",
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 Old Card",
])

_SNAPSHOT_V2 = "\n".join([
    "[metadata]",
    "Name=Test Deck",
    "Moxfield=stable-id",
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 New Card",
])


def _seed_two_iterations(db: Path) -> tuple[int, int]:
    v1 = record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            audit_version="v3", audit_manifest={"added": [], "removed": []},
            sim_report={"total_games": 0}, verdict="kept",
            deck_snapshot=_SNAPSHOT_V1,
        ), db_path=db,
    )
    v2 = record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            parent_id=v1, audit_version="v3",
            audit_manifest={"added": ["New Card"], "removed": ["Old Card"]},
            sim_report={"total_games": 10},
            verdict="reverted", verdict_notes="bad swap",
            deck_snapshot=_SNAPSHOT_V2,
        ), db_path=db,
    )
    return v1, v2


def test_revert_to_iteration_writes_snapshot_to_disk(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)

    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text("contains v2 content", encoding="utf-8")  # current state

    result = revert_to_iteration(v1, deck_path=target, db_path=db)
    assert result.iteration_id == v1
    assert "Old Card" in target.read_text(encoding="utf-8")
    assert "Moxfield=stable-id" in target.read_text(encoding="utf-8")
    # Push blob is the textarea form.
    assert "1 Sol Ring" in result.push_blob


def test_revert_records_revert_iteration_by_default(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text("v2", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db)
    assert result.revert_iteration_id is not None
    history = iterations_for_deck("stable-id", db_path=db)
    assert len(history) == 3   # v1, v2, revert-record
    assert history[-1].audit_version == "revert"
    assert history[-1].audit_manifest["reverted_to_iteration_id"] == v1


def test_revert_skip_record_flag(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    result = revert_to_iteration(v1, deck_path=target, db_path=db, record_revert=False)
    assert result.revert_iteration_id is None
    history = iterations_for_deck("stable-id", db_path=db)
    assert len(history) == 2  # no extra row


def test_revert_to_missing_iteration_raises(tmp_path):
    db = tmp_path / "kl.sqlite"
    with pytest.raises(ValueError):
        revert_to_iteration(99999, deck_path=tmp_path / "x.dck", db_path=db)


def test_revert_to_iteration_without_snapshot_raises(tmp_path):
    db = tmp_path / "kl.sqlite"
    rid = record_iteration(
        Iteration(
            deck_id="d", deck_name="x", bracket=3,
            audit_manifest={"added": [], "removed": []},
            verdict="kept", deck_snapshot=None,
        ), db_path=db,
    )
    with pytest.raises(ValueError, match="no deck_snapshot"):
        revert_to_iteration(rid, deck_path=tmp_path / "x.dck", db_path=db)


def test_revert_deck_to_version_picks_correct_iteration(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    result = revert_deck_to_version(
        "stable-id", version=1, deck_path=target, db_path=db, record_revert=False,
    )
    assert result.iteration_id == v1


def test_revert_deck_to_version_out_of_range(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed_two_iterations(db)
    with pytest.raises(ValueError, match="out of range"):
        revert_deck_to_version(
            "stable-id", version=99, deck_path=tmp_path / "x.dck",
            db_path=db, record_revert=False,
        )


def test_revert_deck_to_version_unknown_deck(tmp_path):
    db = tmp_path / "kl.sqlite"
    with pytest.raises(ValueError, match="no iteration history"):
        revert_deck_to_version(
            "nonexistent", version=1, deck_path=tmp_path / "x.dck",
            db_path=db, record_revert=False,
        )
