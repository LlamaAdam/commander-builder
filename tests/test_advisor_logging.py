"""Tests for the per-recommendation decision logging module.

The logging is opt-in via ``COMMANDER_BUILDER_LOG_DECISIONS``. These
tests pin the wire format + opt-in behavior + no-op-when-disabled
contract.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from commander_builder._advisor_logging import (
    _log_path_for,
    is_enabled,
    log_decisions,
)


def _make_rec(card, action, role=None, source=None, inclusion_pct=None,
              synergy_pct=None, in_n=None, total=None, name_known=True):
    """Build a SwapRecommendation-shaped duck-type for testing."""
    evidence = {}
    if role is not None:
        evidence["role"] = role
    if source is not None:
        evidence["source"] = source
    if inclusion_pct is not None:
        evidence["inclusion_pct"] = inclusion_pct
    if synergy_pct is not None:
        evidence["synergy_pct"] = synergy_pct
    if in_n is not None:
        evidence["in_n_references"] = in_n
    if total is not None:
        evidence["total_references"] = total
    return SimpleNamespace(
        card=card, action=action, evidence=evidence, name_known=name_known,
    )


# ---------------------------------------------------------------------------
# is_enabled + path resolution
# ---------------------------------------------------------------------------

def test_is_enabled_off_by_default(monkeypatch):
    """Production audits should not pay disk I/O. Opt-in only."""
    monkeypatch.delenv("COMMANDER_BUILDER_LOG_DECISIONS", raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
def test_is_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_is_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", value)
    assert is_enabled() is False


def test_log_path_resolves_alongside_other_audit_artifacts(tmp_path):
    """The decision log should land in the same parent dir as
    ``_js_errors.log`` and ``_forge_py_correlation.csv`` so all
    operator-level artifacts cluster together (the project root,
    typically two directories above the deck-file location)."""
    deck_path = (
        tmp_path / "vendor" / "forge" / "userdata" / "decks"
        / "commander" / "fake.dck"
    )
    log_path = _log_path_for(deck_path)
    # parent.parent.parent of decks/commander/<file>.dck → vendor/forge
    assert log_path.name == "_audit_decisions.log"


# ---------------------------------------------------------------------------
# No-op when disabled
# ---------------------------------------------------------------------------

def test_log_decisions_writes_nothing_when_disabled(tmp_path, monkeypatch):
    """The whole point of the env var: zero disk writes off by default."""
    monkeypatch.delenv("COMMANDER_BUILDER_LOG_DECISIONS", raising=False)
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    log_decisions(
        deck_path=deck_path,
        commander_names=["The Ur-Dragon"],
        effective_source="heuristic",
        recommendations=[_make_rec("Sol Ring", "add", role="ramp")],
    )
    log_path = _log_path_for(deck_path)
    assert not log_path.exists()


def test_log_decisions_writes_nothing_when_no_recs(tmp_path, monkeypatch):
    """Empty recs list should also not create the log file."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    log_decisions(
        deck_path=deck_path,
        commander_names=["The Ur-Dragon"],
        effective_source="heuristic",
        recommendations=[],
    )
    log_path = _log_path_for(deck_path)
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

def test_log_decisions_emits_one_line_per_rec(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    # Build the deck-path layout the resolver expects (3 levels deep).
    deck_dir = tmp_path / "a" / "b" / "c"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    recs = [
        _make_rec("Cyclonic Rift", "add", role="wipe",
                  source="edhrec.high_synergy",
                  inclusion_pct=80.0, synergy_pct=15.0),
        _make_rec("Cultivate", "cut", role="ramp", source="edhrec.absence"),
    ]
    log_decisions(
        deck_path=deck_path,
        commander_names=["The Ur-Dragon"],
        effective_source="heuristic",
        recommendations=recs,
    )
    log_path = _log_path_for(deck_path)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Pin the wire format on the first line — key=value pairs joined
    # by single spaces, ``card=`` at the end (multi-word values).
    line = lines[0]
    assert "deck=test.dck" in line
    assert "commander=The Ur-Dragon" in line
    assert "source=heuristic" in line
    assert "action=add" in line
    assert "role=wipe" in line
    assert "evidence_source=edhrec.high_synergy" in line
    assert "match_pct=95" in line  # inclusion 80 + synergy min(15, 20) = 95
    assert "name_known=True" in line
    assert line.endswith("card=Cyclonic Rift")


def test_log_decisions_match_pct_from_reference_frequency(tmp_path, monkeypatch):
    """Bracket-peers recs use in_n/total_references instead of
    inclusion/synergy. Verify the log line computes the percentage
    from that pair when both are set."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    deck_dir = tmp_path / "a" / "b" / "c"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    rec = _make_rec(
        "Moat", "add", role="other", source="bracket_peers",
        in_n=5, total=5,
    )
    log_decisions(
        deck_path=deck_path,
        commander_names=["X"], effective_source="bracket_peers",
        recommendations=[rec],
    )
    log_path = _log_path_for(deck_path)
    line = log_path.read_text(encoding="utf-8").strip()
    assert "match_pct=100" in line


def test_log_decisions_match_pct_none_for_signal_less_rec(tmp_path, monkeypatch):
    """Manabase / vanilla Claude recs carry no scoring signal — the
    log should record ``match_pct=None`` so the field stays present
    (greppable) but doesn't lie about a fake 0% score."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    deck_dir = tmp_path / "a" / "b" / "c"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    rec = _make_rec(
        "Sacred Foundry", "add", role="other", source="manabase_essentials",
    )
    log_decisions(
        deck_path=deck_path,
        commander_names=["X"], effective_source="heuristic",
        recommendations=[rec],
    )
    line = _log_path_for(deck_path).read_text(encoding="utf-8").strip()
    assert "match_pct=None" in line


def test_log_decisions_appends_fallback_event_when_reason_set(
    tmp_path, monkeypatch,
):
    """When ``advise()`` falls back from a requested source to
    heuristic, log a single ``event=fallback`` line so the operator
    can grep for source-availability issues."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    deck_dir = tmp_path / "a" / "b" / "c"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    log_decisions(
        deck_path=deck_path,
        commander_names=["X"], effective_source="heuristic",
        recommendations=[_make_rec("Sol Ring", "add", role="ramp")],
        fallback_reason="claude advisor unavailable: no API key",
    )
    text = _log_path_for(deck_path).read_text(encoding="utf-8")
    assert "event=fallback" in text
    assert "reason=claude advisor unavailable: no API key" in text


def test_log_decisions_appends_across_calls(tmp_path, monkeypatch):
    """Multiple audits in the same process should accumulate lines,
    not truncate the file. The log is append-only."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    deck_dir = tmp_path / "a" / "b" / "c"
    deck_dir.mkdir(parents=True)
    deck_path = deck_dir / "test.dck"
    deck_path.touch()
    for i in range(3):
        log_decisions(
            deck_path=deck_path,
            commander_names=["X"], effective_source="heuristic",
            recommendations=[_make_rec(f"Card {i}", "add", role="ramp")],
        )
    lines = _log_path_for(deck_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_log_decisions_swallows_file_write_errors(tmp_path, monkeypatch):
    """Best-effort: a failed log write must never break the audit
    response. We simulate by pointing the log at a path whose parent
    can't be created (a file masquerading as a directory)."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOG_DECISIONS", "1")
    # Create a file at the project root, then point the deck path
    # such that mkdir(parents=True) on the resolved log_path fails.
    proj_root_as_file = tmp_path / "fake_root"
    proj_root_as_file.write_text("not a dir", encoding="utf-8")
    deck_path = proj_root_as_file / "a" / "b" / "test.dck"
    # Don't try to create it — _log_path_for just does path math.

    # Should not raise.
    log_decisions(
        deck_path=deck_path,
        commander_names=["X"], effective_source="heuristic",
        recommendations=[_make_rec("Card", "add", role="ramp")],
    )
