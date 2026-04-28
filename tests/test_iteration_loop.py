"""iteration_loop unit tests.

The deterministic helper `resolve_deck_id` is unit-tested directly. The
orchestrator `run_one_iteration` hits Forge subprocess via
`compare_versions.compare`; we mock at that boundary so the test stays
offline while still exercising the wiring (compare → analyst → knowledge_log).
"""
from dataclasses import dataclass, field
from typing import Any

import pytest

from commander_builder.compare_versions import ComparisonReport, VersionStats
from commander_builder.iteration_loop import resolve_deck_id, run_one_iteration
from commander_builder.knowledge_log import (
    get_iteration,
    iterations_for_deck,
    stats_summary,
)


def _write_dck(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- resolve_deck_id -------------------------------------------------------

def test_resolve_deck_id_uses_moxfield_metadata(tmp_path):
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck", "\n".join([
        "[metadata]",
        "Name=Foo",
        "Moxfield=abc-XYZ_123",
        "[Commander]",
        "1 Sol Ring",
    ]))
    assert resolve_deck_id(p) == "abc-XYZ_123"


def test_resolve_deck_id_falls_back_to_filename_when_no_metadata(tmp_path):
    p = _write_dck(tmp_path, "[USER] LegacyDeck [B3].dck", "\n".join([
        "[Commander]",
        "1 Atraxa, Praetors' Voice",
    ]))
    # No Moxfield= line + no fallback supplied → use the filename stem.
    out = resolve_deck_id(p)
    # Stem includes the [B3] suffix; that's fine — the goal is just stability.
    assert out == "[USER] LegacyDeck [B3]"


def test_resolve_deck_id_uses_explicit_fallback_over_stem(tmp_path):
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck", "[Commander]\n1 Test")
    assert resolve_deck_id(p, fallback="my-explicit-id") == "my-explicit-id"


def test_resolve_deck_id_metadata_wins_over_fallback(tmp_path):
    """The whole point: Moxfield= is the durable id, even if the caller
    supplies a filename-based fallback."""
    p = _write_dck(tmp_path, "[USER] Renamed Deck [B3].dck", "\n".join([
        "[metadata]",
        "Moxfield=stable-id",
        "[Commander]",
        "1 Test",
    ]))
    assert resolve_deck_id(p, fallback="stale-filename-id") == "stable-id"


def test_resolve_deck_id_strips_trailing_whitespace(tmp_path):
    """Some Moxfield= lines have trailing spaces from the .dck render."""
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck",
                   "[metadata]\nMoxfield=abc-123   \n[Commander]\n1 Test")
    assert resolve_deck_id(p) == "abc-123"


def test_resolve_deck_id_raises_on_missing_file_with_no_fallback(tmp_path):
    with pytest.raises(ValueError):
        resolve_deck_id(tmp_path / "ghost.dck")


def test_resolve_deck_id_uses_fallback_for_missing_file(tmp_path):
    assert resolve_deck_id(tmp_path / "ghost.dck", fallback="emergency-id") == "emergency-id"


# --- run_one_iteration (full orchestrator with mocked compare) -------------

def _make_canned_comparison(
    *,
    old_wins: int,
    new_wins: int,
    draws: int,
    total: int,
) -> ComparisonReport:
    """Build a ComparisonReport that compare_versions.compare would have
    produced. Only the fields run_one_iteration reads matter; everything else
    can stay default."""
    return ComparisonReport(
        old_deck="old.dck",
        new_deck="new.dck",
        bracket=3,
        timestamp="2026-04-26T00:00:00Z",
        mode="pod",
        games_per_pod=10,
        total_games=total,
        draws=draws,
        old_stats=VersionStats(deck_filename="old.dck", wins=old_wins,
                               avg_ending_life=20.0, avg_damage_taken=15.0),
        new_stats=VersionStats(deck_filename="new.dck", wins=new_wins,
                               avg_ending_life=25.0, avg_damage_taken=12.0),
        card_diff={"added": ["NewCard"], "removed": ["OldCard"], "unchanged_count": ["98"]},
    )


@pytest.fixture
def staged_decks(tmp_path, monkeypatch):
    """Stage two .dck files in a fake DECK_DIR + redirect run_one_iteration's
    DECK_DIR to point at it. Both decks share the same Moxfield publicId so
    lineage chains correctly."""
    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)

    v1 = deck_dir / "[USER] Test Deck v1 [B3].dck"
    v1.write_text("\n".join([
        "[metadata]",
        "Name=Test Deck",
        "Moxfield=stable-public-id",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 Sol Ring",
        "1 OldCard",
    ]) + "\n", encoding="utf-8")

    v2 = deck_dir / "[USER] Test Deck v2 [B3].dck"
    v2.write_text("\n".join([
        "[metadata]",
        "Name=Test Deck",
        "Moxfield=stable-public-id",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 Sol Ring",
        "1 NewCard",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr("commander_builder.iteration_loop.DECK_DIR", deck_dir)
    return {"deck_dir": deck_dir, "v1": v1.name, "v2": v2.name}


def test_run_one_iteration_persists_kept_verdict(tmp_path, staged_decks, monkeypatch):
    """Strong improvement (margin 10) → kept verdict → next_action='continue'."""
    canned = _make_canned_comparison(old_wins=2, new_wins=12, draws=0, total=14)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"], "audit_version": "v3"},
        db_path=db,
    )

    assert result.verdict.label == "kept"
    assert result.next_action == "continue"
    assert result.iteration_id > 0

    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched is not None
    assert fetched.deck_id == "stable-public-id"  # publicId, not filename
    assert fetched.verdict == "kept"
    assert fetched.margin == 10
    assert fetched.win_rate_old == round(2 / 14, 3)
    assert fetched.win_rate_new == round(12 / 14, 3)
    assert fetched.audit_manifest["added"] == ["NewCard"]
    # Sim report is the full ComparisonReport.to_dict()
    assert fetched.sim_report["winner"] == "new"


def test_run_one_iteration_persists_reverted_verdict(tmp_path, staged_decks, monkeypatch):
    """Strong regression → reverted → next_action='revert'."""
    canned = _make_canned_comparison(old_wins=12, new_wins=2, draws=0, total=14)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )
    assert result.verdict.label == "reverted"
    assert result.next_action == "revert"


def test_run_one_iteration_handles_inconclusive_draw_heavy_sim(tmp_path, staged_decks, monkeypatch):
    """The Hakbal-vs-Hash case: 18 of 20 games drew. Heuristic returns
    'neutral' (low confidence), iteration_loop should map that to 'stop' so
    the caller knows to ask the user."""
    canned = _make_canned_comparison(old_wins=1, new_wins=1, draws=18, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": [], "removed": []},
        db_path=db,
    )
    assert result.verdict.label == "neutral"
    assert result.next_action == "stop"
    assert "decks_drew_too_often" in str(result.verdict.lessons)


def test_run_one_iteration_chains_via_parent_id(tmp_path, staged_decks, monkeypatch):
    """A v2 → v3 iteration should record parent_id pointing at the v1 → v2
    iteration. Lineage reconstruction is the whole point of GAP-003 + this
    test."""
    canned = _make_canned_comparison(old_wins=2, new_wins=8, draws=0, total=10)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    first = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["X"], "removed": ["Y"]},
        db_path=db,
    )
    second = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["Z"], "removed": ["W"]},
        parent_iteration_id=first.iteration_id,
        db_path=db,
    )

    history = iterations_for_deck("stable-public-id", db_path=db)
    assert len(history) == 2
    assert history[0].id == first.iteration_id
    assert history[1].parent_id == first.iteration_id
    # stats_summary reflects both rows under one deck.
    s = stats_summary(db_path=db)
    assert s["total"] == 2
    assert s["unique_decks"] == 1


def test_run_one_iteration_writes_deck_snapshot_blob(tmp_path, staged_decks, monkeypatch):
    """The .dck text content is preserved in deck_snapshot for reproducibility.
    This is what lets Phase 3 rebuild any historical state without depending
    on Moxfield not deleting the deck."""
    canned = _make_canned_comparison(old_wins=2, new_wins=8, draws=0, total=10)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )

    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.deck_snapshot is not None
    assert "NewCard" in fetched.deck_snapshot
    assert "Moxfield=stable-public-id" in fetched.deck_snapshot
