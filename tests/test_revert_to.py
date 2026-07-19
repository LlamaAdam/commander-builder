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


# --- pre-revert backup ------------------------------------------------------
# revert overwrites the live .dck with a knowledge_log snapshot. If the
# on-disk content was never recorded (manual edit, out-of-band re-pull), the
# overwrite is the ONLY copy being destroyed — these tests pin the copy-aside
# safety net.


def test_revert_backs_up_previous_content(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # Content that exists nowhere in the knowledge log — the unrecoverable case.
    target.write_text("manual edit never recorded", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)

    # Deck file now holds the snapshot...
    assert "Old Card" in target.read_text(encoding="utf-8")
    # ...and the exact pre-revert content survived in the backup.
    assert result.backup_path is not None
    assert result.backup_path.parent == target.parent
    assert (result.backup_path.read_text(encoding="utf-8")
            == "manual edit never recorded")
    # Structured output carries it too (as a string, like restored_path).
    assert result.to_dict()["backup_path"] == str(result.backup_path)


def test_backup_filename_invisible_to_deck_listing_filters(tmp_path):
    """Every deck-listing consumer globs *.dck then (for bracket-aware ones)
    filters on the ` [B<n>].dck` suffix. The backup must match NEITHER, or it
    would show up as a playable deck / pollute counts."""
    from commander_builder.moxfield_import import _existing_moxfield_ids
    from commander_builder.status import _count_decks

    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # Pre-revert content WITH deck-shaped metadata, so if a filter ever did
    # pick the backup up, the assertions below would genuinely fail.
    target.write_text(_SNAPSHOT_V2 + "\n1 Extra Card", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)
    backup = result.backup_path
    assert backup is not None

    # The universal first-stage filter: pathlib glob("*.dck").
    assert backup not in set(tmp_path.glob("*.dck"))
    assert not backup.name.endswith(".dck")
    assert not backup.name.endswith("[B3].dck")

    # Prove it against the real helpers: with the deck file gone, the backup
    # alone must contribute nothing to deck counts or Moxfield-id dedupe
    # (its content DOES contain `Moxfield=stable-id`).
    target.unlink()
    assert _count_decks(tmp_path) == {}
    assert _existing_moxfield_ids(tmp_path, 3) == set()


def test_identical_content_revert_skips_backup(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text(_SNAPSHOT_V1, encoding="utf-8")  # already at v1 state

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)

    # Nothing would have been lost by the overwrite → no backup file created.
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []
    assert result.to_dict()["backup_path"] is None


def test_backup_when_deck_file_absent_is_skipped(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"  # never written

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []


def test_back_to_back_reverts_produce_distinct_backups(tmp_path):
    """Two reverts inside the same wall-clock second must not collide on the
    timestamped backup name — the second would silently overwrite the first,
    which is the exact failure mode the backup exists to prevent."""
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    target.write_text("manual state A", encoding="utf-8")
    r1 = revert_to_iteration(v1, deck_path=target, db_path=db,
                             record_revert=False)
    target.write_text("manual state B", encoding="utf-8")
    r2 = revert_to_iteration(v1, deck_path=target, db_path=db,
                             record_revert=False)

    assert r1.backup_path is not None and r2.backup_path is not None
    assert r1.backup_path != r2.backup_path
    # BOTH pre-revert states survived, in the right files.
    assert r1.backup_path.read_text(encoding="utf-8") == "manual state A"
    assert r2.backup_path.read_text(encoding="utf-8") == "manual state B"


def test_cli_prints_backup_path(tmp_path, monkeypatch, capsys):
    """The backup is only useful if the user is told where it is — pin the
    CLI line without needing a real DB/clipboard behind main()."""
    from commander_builder import revert_to as rt

    fake = rt.RevertResult(
        iteration_id=7,
        restored_path=tmp_path / "deck.dck",
        push_blob="",
        backup_path=tmp_path / "deck.pre-revert-20260719_120000.dck.bak",
    )
    monkeypatch.setattr(rt, "revert_to_iteration",
                        lambda *a, **k: fake)
    monkeypatch.setattr(rt, "prepare_push", lambda *a, **k: None)

    assert rt.main(["--to-iteration", "7", "--no-clipboard"]) == 0
    out = capsys.readouterr().out
    assert str(fake.backup_path) in out

    # And the "nothing to back up" case says so explicitly.
    fake.backup_path = None
    assert rt.main(["--to-iteration", "7", "--no-clipboard"]) == 0
    out = capsys.readouterr().out
    assert "No backup needed" in out
