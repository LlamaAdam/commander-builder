"""knowledge_log SQLite store tests.

Each test uses a tmp_path-scoped DB so they don't pollute the real history.
"""
import pytest

from commander_builder.knowledge_log import (
    Iteration,
    get_iteration,
    init_db,
    iterations_for_deck,
    recent_iterations,
    record_iteration,
    stats_summary,
    update_verdict,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test_knowledge_log.sqlite"
    init_db(p)
    return p


def test_init_db_creates_schema(db):
    # Re-running init is idempotent.
    init_db(db)
    init_db(db)
    summary = stats_summary(db_path=db)
    assert summary["total"] == 0


def test_record_and_get_iteration(db):
    it = Iteration(
        deck_id="abc-XYZ",
        deck_name="Test Deck",
        bracket=3,
        audit_version="v3",
        audit_manifest={"added": ["Sol Ring"], "removed": ["Mind Stone"]},
        win_rate_old=0.4,
        win_rate_new=0.55,
        margin=3,
        deck_snapshot="[Main]\n1 Sol Ring",
    )
    new_id = record_iteration(it, db_path=db)
    assert new_id > 0
    assert it.id == new_id

    fetched = get_iteration(new_id, db_path=db)
    assert fetched is not None
    assert fetched.deck_name == "Test Deck"
    assert fetched.audit_manifest == {"added": ["Sol Ring"], "removed": ["Mind Stone"]}
    assert fetched.win_rate_old == 0.4
    assert fetched.win_rate_new == 0.55


def test_iterations_for_deck_returns_oldest_first(db):
    for i in range(3):
        record_iteration(
            Iteration(deck_id="same-deck", deck_name=f"v{i}", bracket=3),
            db_path=db,
        )
    record_iteration(
        Iteration(deck_id="other-deck", deck_name="other", bracket=4),
        db_path=db,
    )

    same = iterations_for_deck("same-deck", db_path=db)
    assert [it.deck_name for it in same] == ["v0", "v1", "v2"]
    other = iterations_for_deck("other-deck", db_path=db)
    assert len(other) == 1


def test_recent_iterations_returns_newest_first(db):
    for i in range(5):
        record_iteration(
            Iteration(deck_id=f"deck-{i}", deck_name=f"d{i}", bracket=3),
            db_path=db,
        )
    recent = recent_iterations(limit=3, db_path=db)
    assert [it.deck_name for it in recent] == ["d4", "d3", "d2"]


def test_update_verdict_persists(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    update_verdict(new_id, "kept", "win rate +12% over 20 games", db_path=db)

    fetched = get_iteration(new_id, db_path=db)
    assert fetched.verdict == "kept"
    assert fetched.verdict_notes == "win rate +12% over 20 games"


def test_update_verdict_rejects_invalid_value(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    with pytest.raises(ValueError):
        update_verdict(new_id, "garbage", db_path=db)


def test_default_verdict_is_pending(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    fetched = get_iteration(new_id, db_path=db)
    assert fetched.verdict == "pending"


def test_stats_summary_counts_each_verdict(db):
    for _ in range(2):
        rid = record_iteration(Iteration(deck_id="d", deck_name="x", bracket=3), db_path=db)
        update_verdict(rid, "kept", db_path=db)
    rid = record_iteration(Iteration(deck_id="d", deck_name="x", bracket=3), db_path=db)
    update_verdict(rid, "reverted", db_path=db)
    record_iteration(Iteration(deck_id="d2", deck_name="x", bracket=3), db_path=db)  # pending

    s = stats_summary(db_path=db)
    assert s["total"] == 4
    assert s["kept"] == 2
    assert s["reverted"] == 1
    assert s["pending"] == 1
    assert s["unique_decks"] == 2


def test_get_iteration_returns_none_for_missing(db):
    assert get_iteration(99999, db_path=db) is None


def test_iteration_round_trips_complex_manifest(db):
    manifest = {
        "added": ["Foo", "Bar"],
        "removed": ["Baz"],
        "rationale": "Tightened removal package; replaced redundant counterspell",
        "step_4_5_sweep_catches": ["Quux"],
    }
    sim_report = {
        "old_wins": 4, "new_wins": 7, "draws": 9, "total_games": 20,
        "card_diff": {"added": ["Foo", "Bar"], "removed": ["Baz"]},
    }
    rid = record_iteration(
        Iteration(
            deck_id="round-trip-test",
            deck_name="RT Deck",
            bracket=4,
            audit_manifest=manifest,
            sim_report=sim_report,
        ),
        db_path=db,
    )
    fetched = get_iteration(rid, db_path=db)
    assert fetched.audit_manifest == manifest
    assert fetched.sim_report == sim_report


def test_parent_id_chain(db):
    """A v2 iteration points back to its v1 parent. Used by Phase 3 to
    reconstruct the full lineage of each deck for training data."""
    v1_id = record_iteration(
        Iteration(deck_id="lineage", deck_name="v1", bracket=3),
        db_path=db,
    )
    v2_id = record_iteration(
        Iteration(deck_id="lineage", deck_name="v2", bracket=3, parent_id=v1_id),
        db_path=db,
    )
    fetched = get_iteration(v2_id, db_path=db)
    assert fetched.parent_id == v1_id


# --- migrate_legacy_deck_ids -----------------------------------------------

def test_migrate_legacy_deck_ids_updates_filename_style_rows(db):
    """GAP-024: rows with deck_id like '[USER] Foo [B3].dck' should be
    rewritten to use the publicId from the snapshot's Moxfield= line."""
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    snapshot = (
        "[metadata]\n"
        "Name=Foo\n"
        "Moxfield=abc-XYZ\n"
        "[Commander]\n1 Test\n"
    )
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Foo [B3].dck",  # legacy filename-style id
            deck_name="[USER] Foo [B3].dck",
            bracket=3,
            deck_snapshot=snapshot,
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["scanned"] == 1
    assert result["updated"] == 1
    assert result["details"][0]["new_deck_id"] == "abc-XYZ"

    fetched = get_iteration(rid, db_path=db)
    assert fetched.deck_id == "abc-XYZ"


def test_migrate_dry_run_reports_without_writing(db):
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Foo [B3].dck",
            deck_name="x",
            bracket=3,
            deck_snapshot="[metadata]\nMoxfield=abc-XYZ\n[Main]\n1 X",
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db, dry_run=True)
    assert result["dry_run"] is True
    assert result["would_update"] == 1
    assert result["updated"] == 0  # no actual writes

    # Row unchanged on disk.
    assert get_iteration(rid, db_path=db).deck_id == "[USER] Foo [B3].dck"


def test_migrate_skips_rows_without_moxfield_metadata(db):
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Old Deck [B3].dck",
            deck_name="x",
            bracket=3,
            deck_snapshot="[Commander]\n1 Test\n",  # No Moxfield= line
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["updated"] == 0
    assert len(result["skipped"]) == 1
    assert "no Moxfield" in result["skipped"][0]["reason"]
    assert get_iteration(rid, db_path=db).deck_id == "[USER] Old Deck [B3].dck"


def test_migrate_leaves_already_migrated_rows_alone(db):
    """Rows whose deck_id is already a publicId (no `.dck` suffix) shouldn't
    be touched."""
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    record_iteration(
        Iteration(
            deck_id="abc-123",  # already publicId-style
            deck_name="x",
            bracket=3,
            deck_snapshot="Moxfield=def-456",
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["updated"] == 0
    assert result["details"] == []
