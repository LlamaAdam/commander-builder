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


# ---------------------------------------------------------------------------
# Pod-failure surfacing — failed pods must be reported, NOT counted as losses
# ---------------------------------------------------------------------------

def _setup_match_world(tmp_path, monkeypatch):
    """Stage a user deck + 3 opponents in a fake DECK_DIR and route
    run_matchup's pool lookup at them. Mirrors compare_versions'
    _setup_compare_world pattern — mock at the runner boundary only."""
    from commander_builder import run_match as rm

    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    for name in [
        "[USER] Hero [B3].dck",
        "OppA [B3].dck",
        "OppB [B3].dck",
        "OppC [B3].dck",
    ]:
        (deck_dir / name).write_text(
            "[metadata]\nName=" + Path(name).stem + "\n[Main]\n1 Forest\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(rm, "DECK_DIR", deck_dir)
    monkeypatch.setattr(
        rm, "_load_pool",
        lambda bracket: ["OppA [B3].dck", "OppB [B3].dck", "OppC [B3].dck"],
    )
    return rm


def test_run_matchup_excludes_crashed_pod_from_losses(
    tmp_path, monkeypatch, capsys,
):
    """The bug: games from crashed pods (partial per-game lines, no Match
    Result, nonzero rc) entered games_played unattributed and user_losses
    = decisive - wins booked every one of them as a user LOSS. They must
    be excluded and surfaced instead."""
    from commander_builder.forge_runner import SimResult

    rm = _setup_match_world(tmp_path, monkeypatch)

    # Pod 1: healthy — user ("Hero") wins 1 of 3 attributed games.
    good_stdout = (
        "Match Result: Ai(1)-Hero: 1 Ai(2)-OppA [B3]: 2 "
        "Ai(3)-OppB [B3]: 0 Ai(4)-OppC [B3]: 0\n"
        "Game Result: Game 1 ended in 60000 ms. Ai(1)-Hero has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-OppA [B3] has won!\n"
        "Game Result: Game 3 ended in 60000 ms. Ai(2)-OppA [B3] has won!\n"
    )
    # Pod 2: JVM crashed mid-run after 2 games — per-game lines but no
    # trailing Match Result, nonzero exit code.
    crashed_stdout = (
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-OppA [B3] has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(3)-OppB [B3] has won!\n"
    )
    results = iter([
        SimResult(cmd=["x"], returncode=0, duration_sec=1.0,
                  stdout=good_stdout, stderr="", timed_out=False, error=None),
        SimResult(cmd=["x"], returncode=1, duration_sec=0.5,
                  stdout=crashed_stdout, stderr="NPE", timed_out=False,
                  error=None),
    ])

    class FakeRunner:
        def run(self, *args, **kwargs):
            return next(results)

    report = rm.run_matchup(
        user_deck="[USER] Hero [B3].dck",
        bracket=3, games_per_pod=3, num_pods=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_matches",
    )

    # Only pod 1's attributed games count: 1W / 2L, NOT 1W / 4L.
    assert report.games_played == 3
    assert report.user_wins == 1
    assert report.user_losses == 2
    # The failure is flagged, not silently dropped.
    assert report.failed_pods == 1
    assert report.excluded_games == 2
    assert report.pod_failures[0]["reason"] == "Forge exited with code 1"
    assert report.pod_failures[0]["unattributed_games"] == 2
    # Both pods stay in the report (post-mortem data) with flags.
    assert len(report.pods) == 2
    assert report.pods[1]["pod_failed"] is True
    # to_dict carries the failure fields for status.py / report.py readers.
    d = report.to_dict()
    assert d["failed_pods"] == 1
    # Loud console warning + summary line.
    out = capsys.readouterr().out
    assert "FAILED" in out and "EXCLUDED" in out
    s = _format_summary(report)
    assert "1 failed pod(s) EXCLUDED" in s


def test_run_matchup_salvages_timed_out_pod_not_counted_as_losses(
    tmp_path, monkeypatch, capsys,
):
    """A timed-out pod with partial per-game lines but no Match Result gets
    a synthesized summary: finished games are attributed (user's win is
    kept as a WIN, not lost to dilution) and the truncation is flagged."""
    from commander_builder.forge_runner import SimResult

    rm = _setup_match_world(tmp_path, monkeypatch)

    partial = (
        "Game Result: Game 1 ended in 60000 ms. Ai(1)-Hero has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-OppA [B3] has won!\n"
    )

    class FakeRunner:
        def run(self, *args, **kwargs):
            return SimResult(
                cmd=["x"], returncode=None, duration_sec=600.0,
                stdout=partial, stderr="", timed_out=True,
                error="Timed out after 600s",
            )

    report = rm.run_matchup(
        user_deck="[USER] Hero [B3].dck",
        bracket=3, games_per_pod=5, num_pods=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_matches",
    )
    # Pre-fix: games_played=2 with 0 attributed wins → 2 phantom losses.
    assert report.failed_pods == 0
    assert report.timed_out_pods == 1
    assert report.games_played == 2
    assert report.user_wins == 1
    assert report.user_losses == 1
    out = capsys.readouterr().out
    assert "TIMED OUT" in out


def test_run_matchup_timeout_with_nothing_salvageable_is_excluded(
    tmp_path, monkeypatch,
):
    """A pod that hung before any game finished contributes nothing —
    and, critically, no losses."""
    from commander_builder.forge_runner import SimResult

    rm = _setup_match_world(tmp_path, monkeypatch)

    class FakeRunner:
        def run(self, *args, **kwargs):
            return SimResult(
                cmd=["x"], returncode=None, duration_sec=600.0,
                stdout="Turn: Turn 9 (Ai(1)-Hero)\n", stderr="",
                timed_out=True, error="Timed out after 600s",
            )

    report = rm.run_matchup(
        user_deck="[USER] Hero [B3].dck",
        bracket=3, games_per_pod=3, num_pods=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_matches",
    )
    assert report.games_played == 0
    assert report.user_wins == 0
    assert report.user_losses == 0
    assert report.failed_pods == 1
    assert report.pod_failures[0]["timed_out"] is True
