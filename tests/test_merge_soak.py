"""Tests for ``scripts/merge_soak.py`` — the ``--to-knowledge-log`` fold.

Two data-hygiene properties pinned here (2026-07-19):

1. IDEMPOTENCE: the fold used to re-INSERT every 'done' row on every
   run (``record_iteration`` is append-only; nothing deduped), so
   re-running the same merge silently double-counted the dataset the
   FP-002/FP-013 row gates read. The invariant now: the same JSONL row
   folded twice -> one DB row, with a skipped count printed.

2. GAUNTLET ROWS SKIPPED: gauntlet-mode soak rows (written by
   ``soak_pool._record_gauntlet``: per-deck wins/losses vs a FIXED
   3-deck field; no deck_a/deck_b/wins_a/wins_b) also carry
   ``status='done'``, and used to be mangled into bogus ``deck_id='?'``
   0-0 neutral iterations. They are a separate schema and must be
   skipped with a printed count, never folded.

The DB in every test is either an explicit ``--db-path`` under
``tmp_path`` or the conftest-isolated ``DEFAULT_DB_PATH`` — never the
production sqlite (autouse fixture in ``tests/conftest.py``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ isn't a package and isn't on sys.path by default; same import
# convention as test_detune_deck.py / test_margin_analysis.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_soak  # noqa: E402

from commander_builder.knowledge_log import all_iterations  # noqa: E402


# ---------------------------------------------------------------------------
# Row factories — shapes copied from soak_pool._record / _record_gauntlet.
# ---------------------------------------------------------------------------

def _ab_row(**over) -> dict:
    """One AB-schema soak row as soak_pool._record writes it."""
    row = {
        "ts": "2026-07-19T01:02:03.000001+00:00",
        "host": "box1",
        "deck_a": "[USER] Atraxa [B3].dck",
        "deck_b": "[USER] Atraxa v2 [B3].dck",
        "games": 40,
        "wins_a": 12,
        "wins_b": 15,
        "status": "done",
        "duration_sec": 123.4,
        "error": None,
    }
    row.update(over)
    return row


def _gauntlet_row(**over) -> dict:
    """One gauntlet-schema soak row as soak_pool._record_gauntlet writes it.

    Note the distinguishing shape: mode/test_deck/wins/losses present;
    deck_a/deck_b/wins_a/wins_b ABSENT. status is still 'done'.
    """
    row = {
        "ts": "2026-07-19T02:03:04.000001+00:00",
        "host": "box1",
        "mode": "gauntlet",
        "test_deck": "[USER] Atraxa [B3].dck",
        "role": "base",
        "pair_base": "[USER] Atraxa [B3].dck",
        "gauntlet": ["Eldrazi Incursion [M3C] [2024].dck",
                     "Graveyard Overdrive [M3C] [2024].dck",
                     "Creative Energy [M3C] [2024].dck"],
        "games": 40,
        "wins": 13,
        "losses": 25,
        "draws": 2,
        "status": "done",
        "duration_sec": 234.5,
        "error": None,
    }
    row.update(over)
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _merge(tmp_path: Path, inputs: list[str], db: Path) -> int:
    """Run the CLI end-to-end (load + combine + fold) against a tmp DB.

    --out goes into tmp_path so the default combined_soak.jsonl never
    lands in the repo working tree."""
    return merge_soak.main(
        inputs + ["--out", str(tmp_path / "combined.jsonl"),
                  "--db-path", str(db), "--to-knowledge-log"])


# ---------------------------------------------------------------------------
# (a) Idempotence: fold twice -> same row count, skip count printed.
# ---------------------------------------------------------------------------

def test_fold_twice_is_noop(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    # Three DISTINCT sims (unique ts per row, as soak_pool guarantees).
    rows = [
        _ab_row(ts=f"2026-07-19T01:02:0{i}+00:00", wins_a=10 + i, wins_b=20 - i)
        for i in range(3)
    ]
    jsonl = _write_jsonl(tmp_path / "box1.jsonl", rows)

    assert _merge(tmp_path, [str(jsonl)], db) == 0
    assert len(all_iterations(db_path=db)) == 3
    first = capsys.readouterr().out
    assert "wrote 3 iterations" in first
    assert "skipped 0 already-folded" in first

    # Second run of the IDENTICAL merge: must be a no-op on the DB.
    assert _merge(tmp_path, [str(jsonl)], db) == 0
    its = all_iterations(db_path=db)
    assert len(its) == 3, "re-running the same merge must not double-count"
    second = capsys.readouterr().out
    assert "wrote 0 iterations" in second
    assert "skipped 3 already-folded" in second


def test_incremental_refold_only_adds_new_rows(tmp_path, capsys):
    """The real-world flow: the soak appends rows, the merge is re-run.

    Old rows dedupe, only the new tail is written — this is exactly the
    double-count scenario the fix exists for (--append restarts, see
    memory/soak-sims)."""
    db = tmp_path / "kl.sqlite"
    old = [_ab_row(ts="2026-07-19T01:00:00+00:00")]
    jsonl = _write_jsonl(tmp_path / "box1.jsonl", old)
    _merge(tmp_path, [str(jsonl)], db)
    assert len(all_iterations(db_path=db)) == 1
    capsys.readouterr()

    # Soak appended one more sim; the file now holds old + new.
    _write_jsonl(jsonl, old + [_ab_row(ts="2026-07-19T09:00:00+00:00")])
    _merge(tmp_path, [str(jsonl)], db)
    assert len(all_iterations(db_path=db)) == 2
    out = capsys.readouterr().out
    assert "wrote 1 iterations" in out
    assert "skipped 1 already-folded" in out


# ---------------------------------------------------------------------------
# (b) Gauntlet rows: skipped with a count, never a deck_id='?' row.
# ---------------------------------------------------------------------------

def test_gauntlet_rows_skipped_not_mangled(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    jsonl = _write_jsonl(tmp_path / "gauntlet.jsonl",
                         [_gauntlet_row(role="base"),
                          _gauntlet_row(role="v2",
                                        test_deck="[USER] Atraxa v2 [B3].dck",
                                        ts="2026-07-19T03:00:00+00:00")])

    assert _merge(tmp_path, [str(jsonl)], db) == 0
    its = all_iterations(db_path=db)
    # Pre-fix behavior: one deck_id='?' 0-0 neutral row PER gauntlet row.
    assert its == [], "gauntlet rows must not be folded at all"
    assert not [it for it in its if it.deck_id == "?"]
    out = capsys.readouterr().out
    assert "skipped 2 gauntlet-mode rows" in out
    assert "separate schema" in out


def test_gauntlet_shape_detected_without_mode_tag(tmp_path):
    """Belt-and-suspenders: a gauntlet row that lost its ``mode`` tag is
    still recognized by shape (wins/losses present, deck_b absent)."""
    row = _gauntlet_row()
    del row["mode"]
    assert merge_soak._is_gauntlet_row(row)
    # And an AB row is never misclassified.
    assert not merge_soak._is_gauntlet_row(_ab_row())


# ---------------------------------------------------------------------------
# (c) Mixed file: AB rows fold correctly, gauntlet rows don't.
# ---------------------------------------------------------------------------

def test_mixed_file_folds_only_ab_rows(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    ab1 = _ab_row(ts="2026-07-19T01:00:00+00:00", wins_a=12, wins_b=15, games=40)
    ab2 = _ab_row(ts="2026-07-19T02:00:00+00:00",
                  deck_a="[USER] Muldrotha [B3].dck",
                  deck_b="[USER] Muldrotha v2 [B3].dck",
                  wins_a=8, wins_b=8, games=40)
    jsonl = _write_jsonl(tmp_path / "mixed.jsonl",
                         [ab1, _gauntlet_row(), ab2,
                          _gauntlet_row(role="v2",
                                        ts="2026-07-19T04:00:00+00:00")])

    _merge(tmp_path, [str(jsonl)], db)
    its = all_iterations(db_path=db)
    assert len(its) == 2
    assert {it.deck_id for it in its} == {
        "[USER] Atraxa v2 [B3]", "[USER] Muldrotha v2 [B3]"}
    assert not [it for it in its if it.deck_id == "?"]

    # Fold correctness on the Atraxa row: sim_report carries the raw
    # score; win rates use the wins/decisive convention (decisive =
    # wins_a + wins_b, per the knowledge_log schema docstring).
    atraxa = next(it for it in its if "Atraxa" in it.deck_id)
    assert atraxa.audit_version == "soak-ab"
    assert atraxa.sim_report == {"wins_a": 12, "wins_b": 15, "games": 40}
    assert atraxa.win_rate_new == round(15 / 27, 4)
    assert atraxa.win_rate_old == round(12 / 27, 4)
    assert atraxa.margin == 3
    assert atraxa.verdict == "kept"
    # Provenance carried into the manifest, including the sim's own ts
    # (which also anchors refold dedupe).
    assert atraxa.audit_manifest["ts"] == "2026-07-19T01:00:00+00:00"
    assert atraxa.audit_manifest["host"] == "box1"
    assert atraxa.audit_manifest["source"] == "mixed"

    out = capsys.readouterr().out
    assert "wrote 2 iterations" in out
    assert "skipped 2 gauntlet-mode rows" in out


# ---------------------------------------------------------------------------
# (d) Identity must not over-collapse genuinely different sims.
# ---------------------------------------------------------------------------

def test_distinct_sims_with_identical_deck_names_both_fold(tmp_path):
    """Two sims of the SAME pair that even landed the SAME score are
    still two independent samples — soak_pool stamps each with its own
    ``ts``, and that must keep their identities distinct."""
    db = tmp_path / "kl.sqlite"
    rows = [
        _ab_row(ts="2026-07-19T01:00:00+00:00", wins_a=12, wins_b=15),
        _ab_row(ts="2026-07-19T05:00:00+00:00", wins_a=12, wins_b=15),
    ]
    jsonl = _write_jsonl(tmp_path / "box1.jsonl", rows)
    _merge(tmp_path, [str(jsonl)], db)
    assert len(all_iterations(db_path=db)) == 2

    # And the refold of that same file is still a no-op (idempotence
    # holds even when deck names + scores tie).
    _merge(tmp_path, [str(jsonl)], db)
    assert len(all_iterations(db_path=db)) == 2


def test_same_row_under_two_labels_dedupes(tmp_path, capsys):
    """The merge-time ``source`` label is provenance, not sim identity:
    the same sim arriving through two differently-labeled files must
    still fold exactly once.

    The copy in file b re-serializes the row with a DIFFERENT key order:
    byte-identical lines are already dropped by load_tagged's line-level
    dedupe, so identical bytes would never reach the fold — the reordered
    copy sneaks past that guard and pins the fold-level content-identity
    check (and the deliberate exclusion of ``source`` from identity)."""
    db = tmp_path / "kl.sqlite"
    row = _ab_row()
    a = _write_jsonl(tmp_path / "a.jsonl", [row])
    b = _write_jsonl(tmp_path / "b.jsonl",
                     [{k: row[k] for k in reversed(list(row))}])

    _merge(tmp_path, [f"box1={a}", f"box2={b}"], db)
    its = all_iterations(db_path=db)
    assert len(its) == 1
    out = capsys.readouterr().out
    assert "wrote 1 iterations" in out
    assert "skipped 1 already-folded" in out


def test_error_rows_still_ignored(tmp_path):
    """Non-'done' rows (errors/timeouts) never fold — pre-existing
    behavior that must survive the dedupe rework."""
    db = tmp_path / "kl.sqlite"
    jsonl = _write_jsonl(
        tmp_path / "box1.jsonl",
        [_ab_row(status="error", wins_a=None, wins_b=None, games=None,
                 error="Timed out after 360s"),
         _ab_row(ts="2026-07-19T06:00:00+00:00")])
    _merge(tmp_path, [str(jsonl)], db)
    assert len(all_iterations(db_path=db)) == 1
