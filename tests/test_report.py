"""report.py tests — Markdown rendering of iteration lineage."""
from pathlib import Path

import pytest

from commander_builder.knowledge_log import (
    Iteration,
    record_iteration,
    update_verdict,
)
from commander_builder.report import (
    _format_card_diff,
    _format_sim_summary,
    _verdict_badge,
    render_deck_history,
    render_iteration,
    render_recent_iterations_summary,
)


# --- _format_card_diff -----------------------------------------------------

def test_format_card_diff_renders_two_columns():
    md = _format_card_diff({"added": ["A1", "A2"], "removed": ["R1"]})
    assert "| Added | Removed |" in md
    assert "A1" in md
    assert "A2" in md
    assert "R1" in md


def test_format_card_diff_empty():
    assert "_No card changes._" in _format_card_diff({"added": [], "removed": []})


def test_format_card_diff_no_manifest():
    assert "_No manifest recorded._" in _format_card_diff(None)


# --- _format_sim_summary ---------------------------------------------------

def test_format_sim_summary_comparison_report_shape():
    sim = {
        "total_games": 20, "draws": 0,
        "old_stats": {"wins": 8}, "new_stats": {"wins": 12},
        "winner": "new", "margin": 4,
    }
    md = _format_sim_summary(sim)
    assert "OLD 8 – 12 NEW" in md
    assert "Winner" in md and "new" in md
    assert "margin 4" in md


def test_format_sim_summary_matchup_report_shape():
    sim = {
        "games_played": 10, "draws": 2,
        "user_wins": 6, "user_losses": 2, "win_rate": 0.75,
    }
    md = _format_sim_summary(sim)
    assert "6W" in md and "2L" in md
    assert "75" in md  # 0.75 → "75%"


def test_format_sim_summary_no_report():
    assert "_No sim report._" in _format_sim_summary(None)


# --- _verdict_badge --------------------------------------------------------

@pytest.mark.parametrize("verdict,expected_in", [
    ("kept", "KEPT"),
    ("reverted", "REVERTED"),
    ("neutral", "NEUTRAL"),
    ("pending", "PENDING"),
])
def test_verdict_badge_handles_known_labels(verdict, expected_in):
    assert expected_in in _verdict_badge(verdict)


def test_verdict_badge_handles_unknown_label():
    assert "GARBAGE" in _verdict_badge("garbage")


# --- render_iteration ------------------------------------------------------

def _seed_iteration(db: Path, **overrides) -> int:
    defaults = dict(
        deck_id="abc-123", deck_name="Test Deck", bracket=3,
        audit_version="v3",
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"],
                        "rationale": "tightened removal"},
        sim_report={
            "total_games": 20, "draws": 4,
            "old_stats": {"wins": 6}, "new_stats": {"wins": 10},
            "winner": "new", "margin": 4,
        },
        verdict="kept", verdict_notes="clear improvement",
        win_rate_old=0.375, win_rate_new=0.625, margin=4,
    )
    defaults.update(overrides)
    return record_iteration(Iteration(**defaults), db_path=db)


def test_render_iteration_includes_all_sections(tmp_path):
    db = tmp_path / "kl.sqlite"
    rid = _seed_iteration(db)
    from commander_builder.knowledge_log import get_iteration
    it = get_iteration(rid, db_path=db)

    md = render_iteration(it, position=1, total=1)
    assert "Iteration 1/1" in md
    assert "KEPT" in md
    assert "tightened removal" in md
    assert "clear improvement" in md
    assert "NewCard" in md
    assert "OldCard" in md
    assert "OLD 6 – 10 NEW" in md


def test_render_iteration_survives_explicit_null_rationale(tmp_path):
    """A manifest with an explicit JSON null rationale must not crash.

    ``.get("rationale", "")`` returns None (not the default) when the key
    is PRESENT with a null value — the old code then called None.strip()
    and one bad row killed the whole commander-history render.
    """
    db = tmp_path / "kl.sqlite"
    rid = _seed_iteration(db, audit_manifest={"added": ["NewCard"],
                                              "removed": [],
                                              "rationale": None})
    from commander_builder.knowledge_log import get_iteration
    it = get_iteration(rid, db_path=db)

    md = render_iteration(it, position=1, total=1)
    assert "Iteration 1/1" in md
    # No rationale line should appear — null means "no rationale".
    assert "**Rationale**" not in md


def test_render_iteration_skips_missing_optional_fields(tmp_path):
    """No rationale, no verdict_notes, no margin → still renders cleanly."""
    db = tmp_path / "kl.sqlite"
    rid = _seed_iteration(db, audit_manifest={"added": [], "removed": []},
                          verdict_notes=None)
    from commander_builder.knowledge_log import get_iteration
    it = get_iteration(rid, db_path=db)

    md = render_iteration(it, position=1, total=1)
    assert "Iteration 1/1" in md
    assert "_No card changes._" in md


# --- render_deck_history ---------------------------------------------------

def test_render_deck_history_empty_for_unknown_deck(tmp_path):
    db = tmp_path / "kl.sqlite"
    md = render_deck_history("nonexistent", db_path=db)
    assert "No iterations" in md
    assert "nonexistent" in md


def test_render_deck_history_shows_lineage(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1 = _seed_iteration(db, verdict="neutral",
                         audit_manifest={"added": [], "removed": []})
    v2 = _seed_iteration(db, parent_id=v1, verdict="kept")
    v3 = _seed_iteration(db, parent_id=v2, verdict="reverted")

    md = render_deck_history("abc-123", db_path=db)
    assert "Test Deck" in md
    assert "Iterations**: 3" in md
    assert "Iteration 1/3" in md
    assert "Iteration 2/3" in md
    assert "Iteration 3/3" in md
    # Verdict tally line.
    assert "kept: 1" in md
    assert "reverted: 1" in md
    assert "neutral: 1" in md


def test_render_deck_history_shows_win_rate_trajectory(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed_iteration(db, win_rate_old=0.4, win_rate_new=0.5)
    _seed_iteration(db, win_rate_old=0.5, win_rate_new=0.6)
    _seed_iteration(db, win_rate_old=0.6, win_rate_new=0.7)

    md = render_deck_history("abc-123", db_path=db)
    # Should show the journey from first measured (50%) to last (70%).
    assert "50% → 70%" in md
    assert "+20%" in md


def test_render_deck_history_handles_no_measured_win_rates(tmp_path):
    """If every iteration has win_rate_new=None, the trajectory section
    should be skipped, not crash."""
    db = tmp_path / "kl.sqlite"
    _seed_iteration(db, win_rate_old=None, win_rate_new=None)
    md = render_deck_history("abc-123", db_path=db)
    # Section omitted; no crash.
    assert "Win-rate trajectory" not in md


# --- render_recent_iterations_summary --------------------------------------

def test_render_recent_iterations_summary_empty(tmp_path):
    db = tmp_path / "kl.sqlite"
    md = render_recent_iterations_summary(db_path=db)
    assert "No iterations recorded" in md


def test_render_recent_iterations_summary_renders_table(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed_iteration(db, deck_name="Deck A", deck_id="a")
    _seed_iteration(db, deck_name="Deck B", deck_id="b", verdict="reverted",
                    margin=-5)
    md = render_recent_iterations_summary(db_path=db)
    assert "Recent iterations" in md
    assert "Deck A" in md
    assert "Deck B" in md
    assert "-5" in md  # negative margin formatted


def test_render_recent_respects_limit(tmp_path):
    db = tmp_path / "kl.sqlite"
    for i in range(10):
        _seed_iteration(db, deck_name=f"D{i}", deck_id=f"deck-{i}")
    md = render_recent_iterations_summary(limit=3, db_path=db)
    # Only the 3 most recent appear.
    assert "Recent iterations (last 3)" in md
    assert "D9" in md
    assert "D0" not in md
