"""Tests for scripts/seed_demo_knowledge_log.py — FP-006 demo seeder."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest


def _load_seed_module():
    """Import the script as a module despite its non-package location."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_knowledge_log.py"
    spec = importlib.util.spec_from_file_location("seed_demo_knowledge_log", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seed_mod():
    return _load_seed_module()


def test_seed_demo_writes_four_iterations(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    last_id = seed_mod.seed_demo(db, deck_id="test-deck")
    assert db.exists()
    assert last_id == 4

    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM iterations ORDER BY id"))
    assert len(rows) == 4

    verdicts = [r["verdict"] for r in rows]
    # Mockup-aligned arc: pending baseline → kept → reverted → neutral.
    assert verdicts == ["pending", "kept", "reverted", "neutral"]


def test_seed_demo_chains_parent_ids(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    seed_mod.seed_demo(db)

    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT id, parent_id FROM iterations ORDER BY id"))
    # First has no parent; rest chain.
    assert rows[0]["parent_id"] is None
    for prev, curr in zip(rows, rows[1:]):
        assert curr["parent_id"] == prev["id"]


def test_seed_demo_records_win_rate_curve(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    seed_mod.seed_demo(db)

    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        rates = [r["win_rate_new"] for r in
                 conn.execute("SELECT win_rate_new FROM iterations ORDER BY id")]
    assert rates[0] == pytest.approx(0.41)
    assert rates[1] == pytest.approx(0.52)
    # Reverted experiment dipped.
    assert rates[2] < rates[1]
    # Neutral retest stayed near v2's level.
    assert rates[3] == pytest.approx(0.54)


def test_seed_demo_includes_deck_snapshot(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    seed_mod.seed_demo(db)

    with closing(sqlite3.connect(str(db))) as conn:
        snapshots = [r[0] for r in conn.execute("SELECT deck_snapshot FROM iterations")]
    for snap in snapshots:
        assert "[Commander]" in snap
        assert "Omnath, Locus of Creation" in snap


def test_seed_demo_uses_passed_deck_id(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    seed_mod.seed_demo(db, deck_id="my-custom-id")

    with closing(sqlite3.connect(str(db))) as conn:
        ids = {r[0] for r in conn.execute("SELECT DISTINCT deck_id FROM iterations")}
    assert ids == {"my-custom-id"}


def test_main_with_force_overwrites(tmp_path, seed_mod):
    db = tmp_path / "demo.sqlite"
    db.write_text("not a real sqlite file")
    rc = seed_mod.main(["--db", str(db), "--force"])
    assert rc == 0
    # New file should be a valid sqlite db with 4 rows.
    with closing(sqlite3.connect(str(db))) as conn:
        count = conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
    assert count == 4
