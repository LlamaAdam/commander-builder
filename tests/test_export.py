"""export.py tests — round-trip + filter behavior."""
import json
from pathlib import Path

import pytest

from commander_builder.export import (
    SCHEMA_VERSION,
    export_knowledge_log,
    import_knowledge_log,
)
from commander_builder.knowledge_log import (
    Iteration,
    iterations_for_deck,
    record_iteration,
    stats_summary,
)


def _seed(
    db: Path,
    deck_id: str,
    deck_name: str = "Deck",
    verdict: str = "kept",
    created_at: str = None,
    parent_id: int = None,
) -> int:
    # created_at is normally stamped at insert; tests that need two DBs to
    # hold the SAME iteration (content-identity dedupe) pass it explicitly.
    return record_iteration(
        Iteration(
            deck_id=deck_id, deck_name=deck_name, bracket=3,
            audit_version="v3",
            audit_manifest={"added": ["A"], "removed": ["B"], "rationale": "x"},
            sim_report={"total_games": 10, "draws": 0,
                        "old_stats": {"wins": 4}, "new_stats": {"wins": 6}},
            verdict=verdict, win_rate_old=0.4, win_rate_new=0.6, margin=2,
            deck_snapshot="[Commander]\n1 Test\n[Main]\n1 Foo\n",
            created_at=created_at,
            parent_id=parent_id,
        ),
        db_path=db,
    )


# --- export_knowledge_log --------------------------------------------------

def test_export_full_dump_writes_all_rows(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed(db, "deck-a")
    _seed(db, "deck-a")
    _seed(db, "deck-b")

    out = tmp_path / "dump.json"
    payload = export_knowledge_log(out, db_path=db)
    assert out.exists()
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert len(on_disk["iterations"]) == 3
    assert on_disk["scope"] == "all"
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert "exported_at" in on_disk


def test_export_filtered_by_deck_id(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed(db, "deck-a")
    _seed(db, "deck-a")
    _seed(db, "deck-b")

    out = tmp_path / "deck_a.json"
    payload = export_knowledge_log(out, deck_id="deck-a", db_path=db)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert len(on_disk["iterations"]) == 2
    assert all(it["deck_id"] == "deck-a" for it in on_disk["iterations"])
    assert on_disk["scope"] == "deck_id=deck-a"


def test_export_recent_limits_count(tmp_path):
    db = tmp_path / "kl.sqlite"
    for i in range(10):
        _seed(db, f"deck-{i}")

    out = tmp_path / "recent.json"
    export_knowledge_log(out, recent=3, db_path=db)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["iterations"]) == 3


def test_export_full_dump_sorts_by_id(tmp_path):
    db = tmp_path / "kl.sqlite"
    ids = [_seed(db, f"d-{i}") for i in range(5)]

    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=db)
    payload = json.loads(out.read_text(encoding="utf-8"))
    ids_in_export = [it["id"] for it in payload["iterations"]]
    assert ids_in_export == sorted(ids_in_export)


def test_export_creates_parent_directory(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed(db, "d")
    out = tmp_path / "nested" / "subdir" / "dump.json"
    export_knowledge_log(out, db_path=db)
    assert out.exists()


def test_export_includes_stats_at_export(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed(db, "d", verdict="kept")
    _seed(db, "d", verdict="reverted")
    out = tmp_path / "dump.json"
    payload = export_knowledge_log(out, db_path=db)
    assert payload["stats_at_export"]["total"] == 2
    assert payload["stats_at_export"]["kept"] == 1
    assert payload["stats_at_export"]["reverted"] == 1


# --- import_knowledge_log --------------------------------------------------

def test_import_round_trip_preserves_data(tmp_path):
    src = tmp_path / "src.sqlite"
    _seed(src, "deck-a", deck_name="Deck A")
    _seed(src, "deck-b", deck_name="Deck B")

    export_path = tmp_path / "dump.json"
    export_knowledge_log(export_path, db_path=src)

    dst = tmp_path / "dst.sqlite"
    result = import_knowledge_log(export_path, db_path=dst)
    assert result["inserted"] == 2
    assert result["skipped"] == 0

    a_history = iterations_for_deck("deck-a", db_path=dst)
    b_history = iterations_for_deck("deck-b", db_path=dst)
    assert len(a_history) == 1
    assert len(b_history) == 1
    assert a_history[0].deck_name == "Deck A"


def test_import_skips_existing_rows_by_default(tmp_path):
    """If the destination already has the row's id, the import skips it
    rather than creating a duplicate with a new id."""
    db = tmp_path / "kl.sqlite"
    _seed(db, "deck-a")
    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=db)

    # Re-import into the same DB.
    result = import_knowledge_log(out, db_path=db)
    assert result["skipped"] == 1
    assert result["inserted"] == 0
    # Total count unchanged.
    assert stats_summary(db_path=db)["total"] == 1


def test_import_no_skip_existing_inserts_duplicates(tmp_path):
    """With skip_existing=False, the row is re-inserted with a fresh id
    (intentionally duplicates data — caller wanted that)."""
    db = tmp_path / "kl.sqlite"
    _seed(db, "deck-a")
    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=db)

    result = import_knowledge_log(out, db_path=db, skip_existing=False)
    assert result["inserted"] == 1
    assert result["skipped"] == 0
    assert stats_summary(db_path=db)["total"] == 2


def test_import_preserves_audit_manifest_and_snapshot(tmp_path):
    src = tmp_path / "src.sqlite"
    _seed(src, "deck-a")
    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)

    dst = tmp_path / "dst.sqlite"
    import_knowledge_log(out, db_path=dst)
    history = iterations_for_deck("deck-a", db_path=dst)
    it = history[0]
    assert it.audit_manifest == {"added": ["A"], "removed": ["B"], "rationale": "x"}
    assert "1 Foo" in (it.deck_snapshot or "")


# --- content-identity merge (id-collision data-loss regression) ------------

def test_import_merges_overlapping_ids_with_different_content(tmp_path):
    """Two machines' DBs both start autoincrement ids at 1. Merging must not
    drop imported rows just because their id collides with an unrelated
    local row — the historical bug silently skipped nearly everything."""
    src = tmp_path / "machine_a.sqlite"
    _seed(src, "deck-a1", deck_name="A One")
    _seed(src, "deck-a2", deck_name="A Two")

    dst = tmp_path / "machine_b.sqlite"
    _seed(dst, "deck-b1", deck_name="B One")
    _seed(dst, "deck-b2", deck_name="B Two")

    out = tmp_path / "a_dump.json"
    export_knowledge_log(out, db_path=src)
    # Sanity: the export really does carry ids 1 and 2, colliding with dst.
    exported_ids = [r["id"] for r in json.loads(out.read_text(encoding="utf-8"))["iterations"]]
    assert exported_ids == [1, 2]

    result = import_knowledge_log(out, db_path=dst)
    assert result["inserted"] == 2
    assert result["skipped"] == 0
    assert result["skipped_identical"] == 0
    # Both imported rows landed under fresh local ids (3, 4), not 1, 2.
    assert result["id_remapped"] == 2
    # Lossless: all four decks' histories are present.
    assert stats_summary(db_path=dst)["total"] == 4
    for deck in ("deck-a1", "deck-a2", "deck-b1", "deck-b2"):
        assert len(iterations_for_deck(deck, db_path=dst)) == 1


def test_import_dedupes_identical_content_across_different_ids(tmp_path):
    """The same iteration living at DIFFERENT ids in the two DBs is still
    recognized as one iteration (identity is content, not id)."""
    ts = "2026-07-19T12:00:00+00:00"
    src = tmp_path / "src.sqlite"
    _seed(src, "filler", deck_name="Filler")           # src id 1
    _seed(src, "shared", deck_name="Shared", created_at=ts)  # src id 2

    dst = tmp_path / "dst.sqlite"
    _seed(dst, "shared", deck_name="Shared", created_at=ts)  # dst id 1

    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)
    result = import_knowledge_log(out, db_path=dst)
    assert result["skipped_identical"] == 1  # the shared row, despite id 2 vs 1
    assert result["inserted"] == 1           # the filler row
    assert stats_summary(db_path=dst)["total"] == 2
    assert len(iterations_for_deck("shared", db_path=dst)) == 1


def test_import_same_identity_different_verdict_skips_local_wins(tmp_path):
    """Same iteration (same natural key) but the export was taken before a
    local verdict PATCH — treated as the same iteration; local row wins."""
    ts = "2026-07-19T12:00:00+00:00"
    src = tmp_path / "src.sqlite"
    _seed(src, "deck-x", created_at=ts, verdict="pending")

    dst = tmp_path / "dst.sqlite"
    _seed(dst, "deck-x", created_at=ts, verdict="kept")

    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)
    result = import_knowledge_log(out, db_path=dst)
    assert result["inserted"] == 0
    assert result["skipped_existing_variant"] == 1
    history = iterations_for_deck("deck-x", db_path=dst)
    assert len(history) == 1
    assert history[0].verdict == "kept"  # local verdict untouched


def test_import_remaps_parent_ids_to_local_ids(tmp_path):
    """A v1→v2 chain from another machine must keep pointing at ITS OWN v1
    after import, not at whatever unrelated local row owns the source id."""
    src = tmp_path / "src.sqlite"
    root = _seed(src, "chain", deck_name="Chain v1")          # src id 1
    _seed(src, "chain", deck_name="Chain v2", parent_id=root)  # src id 2, parent 1

    dst = tmp_path / "dst.sqlite"
    _seed(dst, "unrelated-1")  # dst id 1 — would be the WRONG parent
    _seed(dst, "unrelated-2")  # dst id 2

    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)
    result = import_knowledge_log(out, db_path=dst)
    assert result["inserted"] == 2
    assert result["unresolved_parents"] == 0

    chain = iterations_for_deck("chain", db_path=dst)
    assert len(chain) == 2
    v1, v2 = chain
    assert v1.parent_id is None
    assert v2.parent_id == v1.id  # remapped to the NEW local id (3), not 1
    assert v2.parent_id != 1


def test_import_nulls_parent_ids_it_cannot_resolve(tmp_path):
    """A row whose parent isn't in the export (e.g. a deck-filtered export
    of v2 only) must not carry a foreign parent id into the local DB."""
    src = tmp_path / "src.sqlite"
    root = _seed(src, "chain", deck_name="Chain v1")
    _seed(src, "chain", deck_name="Chain v2", parent_id=root)

    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)
    # Drop v1 from the export file to simulate a partial export.
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["iterations"] = [r for r in payload["iterations"] if r["deck_name"] == "Chain v2"]
    out.write_text(json.dumps(payload), encoding="utf-8")

    dst = tmp_path / "dst.sqlite"
    _seed(dst, "unrelated")  # dst id 1 == the orphan's source parent id
    result = import_knowledge_log(out, db_path=dst)
    assert result["inserted"] == 1
    assert result["unresolved_parents"] == 1
    orphan = iterations_for_deck("chain", db_path=dst)[0]
    assert orphan.parent_id is None  # not pointing at "unrelated"


def test_reimport_same_file_is_noop(tmp_path):
    """Round-trip: export A, import into fresh B, then re-import the same
    file — the second import must be a complete no-op (all identical)."""
    src = tmp_path / "src.sqlite"
    _seed(src, "deck-a")
    _seed(src, "deck-b")
    out = tmp_path / "dump.json"
    export_knowledge_log(out, db_path=src)

    dst = tmp_path / "dst.sqlite"
    first = import_knowledge_log(out, db_path=dst)
    assert first["inserted"] == 2

    second = import_knowledge_log(out, db_path=dst)
    assert second["inserted"] == 0
    assert second["skipped_identical"] == 2
    assert second["skipped"] == 2
    assert stats_summary(db_path=dst)["total"] == 2


def test_export_full_dump_has_no_10k_cap(tmp_path):
    """The full export used to silently truncate at 10,000 rows. Bulk-insert
    past the old cap with raw SQL (record_iteration per-row would be slow)
    and assert every row makes it out."""
    from commander_builder.knowledge_log import _connect, init_db

    db = tmp_path / "big.sqlite"
    init_db(db_path=db)
    n = 10_050
    with _connect(db) as conn:
        conn.executemany(
            "INSERT INTO iterations (deck_id, deck_name, bracket, verdict, created_at) "
            "VALUES (?, ?, 3, 'kept', ?)",
            [(f"deck-{i}", f"Deck {i}", f"2026-01-01T00:00:{i:02d}") for i in range(n)],
        )

    out = tmp_path / "big.json"
    payload = export_knowledge_log(out, db_path=db)
    assert len(payload["iterations"]) == n
    # Still oldest-first so re-import preserves chain order.
    ids = [r["id"] for r in payload["iterations"]]
    assert ids == sorted(ids)


def test_export_empty_db_returns_empty_iterations(tmp_path):
    db = tmp_path / "kl.sqlite"
    out = tmp_path / "empty.json"
    payload = export_knowledge_log(out, db_path=db)
    assert payload["iterations"] == []
    assert payload["stats_at_export"]["total"] == 0
