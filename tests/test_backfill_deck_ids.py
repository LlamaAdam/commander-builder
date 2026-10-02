"""Tests for scripts/backfill_deck_ids.py — the one-off re-key of
per-version filename stems onto the stable deck identity (R3 C-08).

Builds a temp knowledge_log with rows in every id shape, then checks:

  * dry-run reports the changes without writing anything;
  * --apply rewrites exactly the filename-shaped, non-stable rows;
  * explicit ids (publicId, test id, archidekt:) are NEVER touched;
  * the snapshot's provenance line wins over the stem;
  * parent_id and every measurement column are untouched;
  * the rewrite is idempotent.

No network, no Forge — pure SQLite in tmp_path.
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_deck_ids as bdi  # noqa: E402

from commander_builder.knowledge_log import init_db  # noqa: E402


def _insert(db: Path, row_id: int, deck_id: str, snapshot=None, parent=None) -> None:
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "INSERT INTO iterations (id, deck_id, deck_name, bracket, verdict, "
            "margin, parent_id, deck_snapshot, created_at) "
            "VALUES (?, ?, ?, 3, 'kept', 4, ?, ?, '2026-08-20T00:00:00+00:00')",
            (row_id, deck_id, deck_id, parent, snapshot),
        )
        conn.commit()


def _rows(db: Path) -> dict:
    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        return {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, deck_id, parent_id, margin, verdict FROM iterations")}


@pytest.fixture
def seeded_db(tmp_path) -> Path:
    db = tmp_path / "klog.sqlite"
    init_db(db)
    _insert(db, 1, "abc-publicId")                                   # explicit: untouched
    _insert(db, 2, "[USER] Hand Deck v2 [B3]")                       # versioned stem
    _insert(db, 3, "[USER] Hand Deck v3 [B3]", parent=2)             # versioned stem, chained
    _insert(db, 4, "[USER] Ark v2 [B3]",
            snapshot="[metadata]\nArchidekt=99\nSource=archidekt\n")  # provenance wins
    _insert(db, 5, "[USER] Legacy [B3].dck",
            snapshot="[metadata]\nMoxfield=mox-1\n")                 # legacy filename + Moxfield
    _insert(db, 6, "[USER] Stable [B3]")                             # already stable
    _insert(db, 7, "archidekt:12")                                   # explicit: untouched
    return db


def test_dry_run_reports_but_does_not_write(seeded_db):
    before = _rows(seeded_db)
    summary = bdi.backfill(seeded_db, apply=False)
    assert summary["applied"] is False
    assert {c["id"] for c in summary["changes"]} == {2, 3, 4, 5}
    assert summary["skipped"] == 2          # ids 1 and 7
    assert summary["unchanged"] == 1        # id 6
    assert _rows(seeded_db) == before


def test_apply_rewrites_exactly_the_candidates(seeded_db):
    bdi.backfill(seeded_db, apply=True)
    rows = _rows(seeded_db)
    assert rows[2]["deck_id"] == rows[3]["deck_id"] == "[USER] Hand Deck [B3]"
    assert rows[4]["deck_id"] == "archidekt:99"
    assert rows[5]["deck_id"] == "mox-1"
    assert rows[1]["deck_id"] == "abc-publicId"
    assert rows[6]["deck_id"] == "[USER] Stable [B3]"
    assert rows[7]["deck_id"] == "archidekt:12"


def test_apply_touches_no_other_column(seeded_db):
    bdi.backfill(seeded_db, apply=True)
    rows = _rows(seeded_db)
    assert rows[3]["parent_id"] == 2
    assert all(r["margin"] == 4 and r["verdict"] == "kept" for r in rows.values())


def test_apply_is_idempotent(seeded_db):
    bdi.backfill(seeded_db, apply=True)
    second = bdi.backfill(seeded_db, apply=True)
    assert second["changes"] == []


def test_rekeyed_rows_are_one_deck_to_the_library(seeded_db):
    """The point of the exercise: per-deck surfaces see one deck again."""
    from commander_builder.knowledge_log import iterations_for_deck
    bdi.backfill(seeded_db, apply=True)
    assert [r.id for r in iterations_for_deck("[USER] Hand Deck [B3]", db_path=seeded_db)] == [2, 3]


def test_main_dry_run_prints_table(seeded_db, capsys):
    rc = bdi.main(["--db", str(seeded_db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    assert "[USER] Hand Deck v2 [B3]" in out and "[USER] Hand Deck [B3]" in out
    assert _rows(seeded_db)[2]["deck_id"] == "[USER] Hand Deck v2 [B3]"


def test_main_apply_writes(seeded_db, capsys):
    rc = bdi.main(["--db", str(seeded_db), "--apply"])
    assert rc == 0
    assert "APPLIED" in capsys.readouterr().out
    assert _rows(seeded_db)[2]["deck_id"] == "[USER] Hand Deck [B3]"


def test_main_errors_on_missing_db(tmp_path, capsys):
    rc = bdi.main(["--db", str(tmp_path / "nope.sqlite")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err
