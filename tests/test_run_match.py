"""run_match unit tests for the pure-Python helpers.

The actual matchup loop hits Forge subprocess, so we test only the deterministic
helpers here: pod construction, fallback opponent selection, summary formatting,
and the win-rate property.
"""
import json
from pathlib import Path

import pytest

from commander_builder.run_match import (
    MatchupReport,
    _build_pods,
    _format_summary,
    _load_pool,
)


def test_build_pods_each_has_user_plus_three_opponents():
    pods = _build_pods("U.dck", [f"o{i}" for i in range(6)], num_pods=2)
    for pod in pods:
        assert len(pod) == 4
        assert pod[0] == "U.dck"
        # No duplicates within a pod (user can't share a slot with itself).
        assert len(set(pod)) == 4


def test_build_pods_rotates_opponents_across_pods():
    pods = _build_pods("U.dck", [f"o{i}" for i in range(6)], num_pods=2)
    seen: set[str] = set()
    for pod in pods:
        seen.update(pod[1:])
    # With 6 opponents and 2 pods (3 slots each), we should reach all 6.
    assert seen == {f"o{i}" for i in range(6)}


def test_build_pods_handles_fewer_opponents_than_3x_pods():
    # 3 opponents, 3 pods — each pod must repeat the same trio rather than crash.
    pods = _build_pods("U.dck", ["o1", "o2", "o3"], num_pods=3)
    assert len(pods) == 3
    for pod in pods:
        assert set(pod[1:]) == {"o1", "o2", "o3"}


def test_build_pods_rejects_too_few_opponents():
    with pytest.raises(ValueError):
        _build_pods("U.dck", ["o1", "o2"], num_pods=1)


def test_load_pool_returns_empty_when_missing(tmp_path):
    assert _load_pool(99, pool_dir=tmp_path) == []


def test_load_pool_concatenates_pool_a_and_b(tmp_path):
    payload = {
        "bracket": 3,
        "pool_a": ["a1", "a2", "a3"],
        "pool_b": ["b1", "b2", "b3"],
    }
    (tmp_path / "B3.json").write_text(json.dumps(payload), encoding="utf-8")
    assert _load_pool(3, pool_dir=tmp_path) == ["a1", "a2", "a3", "b1", "b2", "b3"]


def test_win_rate_excludes_draws():
    r = MatchupReport(
        user_deck="U.dck", bracket=3, timestamp="x",
        games_played=10, user_wins=4, user_losses=4, draws=2,
    )
    # Decisive = 8, wins = 4 → 0.5
    assert r.win_rate == 0.5


def test_win_rate_zero_when_all_draws():
    r = MatchupReport(
        user_deck="U.dck", bracket=3, timestamp="x",
        games_played=3, user_wins=0, user_losses=0, draws=3,
    )
    assert r.win_rate == 0.0


def test_to_dict_includes_win_rate():
    r = MatchupReport(
        user_deck="U.dck", bracket=3, timestamp="x",
        games_played=4, user_wins=3, user_losses=1, draws=0,
    )
    d = r.to_dict()
    assert d["win_rate"] == 0.75


def test_format_summary_includes_record_line():
    r = MatchupReport(
        user_deck="U.dck", bracket=3, timestamp="x",
        games_played=6, user_wins=4, user_losses=2, draws=0,
        avg_user_ending_life=22.0, avg_user_damage_taken=18.0,
        avg_turns_when_won=12.5, avg_turns_when_lost=14.0,
    )
    s = _format_summary(r)
    assert "U.dck" in s
    assert "4W" in s and "2L" in s
    assert "win rate 66.7%" in s
