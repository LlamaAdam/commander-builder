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


def _seed(db: Path, deck_id: str, deck_name: str = "Deck", verdict: str = "kept") -> int:
    return record_iteration(
        Iteration(
            deck_id=deck_id, deck_name=deck_name, bracket=3,
            audit_version="v3",
            audit_manifest={"added": ["A"], "removed": ["B"], "rationale": "x"},
            sim_report={"total_games": 10, "draws": 0,
                        "old_stats": {"wins": 4}, "new_stats": {"wins": 6}},
            verdict=verdict, win_rate_old=0.4, win_rate_new=0.6, margin=2,
            deck_snapshot="[Commander]\n1 Test\n[Main]\n1 Foo\n",
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


def test_export_empty_db_returns_empty_iterations(tmp_path):
    db = tmp_path / "kl.sqlite"
    out = tmp_path / "empty.json"
    payload = export_knowledge_log(out, db_path=db)
    assert payload["iterations"] == []
    assert payload["stats_at_export"]["total"] == 0
