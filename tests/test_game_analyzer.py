"""Game-level analyzer tests.

Pins the per-game telemetry contracts so changes to log_parser or to the
analyzer's regex set surface immediately. Uses synthetic Forge-style stdout
fragments — each test is a minimal log slice exercising one feature.
"""
import json
from pathlib import Path

import pytest

from commander_builder.game_analyzer import (
    GameAnalysis,
    MatchAnalysis,
    analyze,
)


def _game_log(end_turn: int, winner_seat: int, winner_name: str, duration_ms: int = 60000) -> str:
    """Build a minimal one-game log: turn marker, life ticks, outcome, result."""
    return (
        f"Turn: Turn 1 (Ai({winner_seat})-{winner_name})\n"
        f"Life: Life: Ai(1)-A 40 > 35\n"
        f"Life: Life: Ai(2)-B 40 > 0\n"
        f"Game Outcome: Turn {end_turn}\n"
        f"Game Outcome: Ai({winner_seat})-{winner_name} has won\n"
        f"Game Result: Game 1 ended in {duration_ms} ms. "
        f"Ai({winner_seat})-{winner_name} has won!\n"
    )


def test_empty_input_returns_empty_match():
    ma = analyze("")
    assert ma.total_games == 0
    assert ma.draws == 0
    assert ma.avg_turns == 0.0


def test_single_game_extracts_winner_and_turn():
    log = _game_log(end_turn=12, winner_seat=1, winner_name="Foo Deck", duration_ms=72000)
    ma = analyze(log)
    assert ma.total_games == 1
    g = ma.games[0]
    assert g.end_turn == 12
    assert g.winner_seat == 1
    assert g.winner_name == "Foo Deck"
    assert g.is_draw is False
    assert g.duration_ms == 72000
    assert g.duration_sec == 72.0


def test_life_tracking_per_deck():
    # Turn markers advance through the game — life events fire DURING a turn,
    # so the analyzer attributes elimination to the most recent Turn marker.
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Life: Life: Ai(1)-A 40 > 38\n"
        "Turn: Turn 2 (Ai(2)-B)\n"
        "Life: Life: Ai(1)-A 38 > 36\n"
        "Life: Life: Ai(1)-A 36 > 38\n"  # +2 lifegain
        "Turn: Turn 5 (Ai(1)-A)\n"
        "Life: Life: Ai(2)-B 40 > 0\n"
        "Game Outcome: Turn 5\n"
        "Game Result: Game 1 ended in 30000 ms. Ai(1)-A has won!\n"
    )
    ma = analyze(log)
    g = ma.games[0]
    a = next(d for d in g.deck_stats if d.seat == 1)
    b = next(d for d in g.deck_stats if d.seat == 2)
    assert a.starting_life == 40
    assert a.ending_life == 38
    assert a.damage_taken == 4   # 40>38 (-2) + 38>36 (-2)
    assert a.life_gained == 2    # 36>38
    assert a.eliminated is False
    assert b.eliminated is True
    assert b.eliminated_turn == 5


def test_draw_marker_detected():
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Life: Life: Ai(1)-A 40 > 35\n"
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"  # no "has won!" clause
    )
    ma = analyze(log)
    assert ma.draws == 1
    g = ma.games[0]
    assert g.is_draw is True
    assert g.winner_name is None


def test_multi_game_split_at_game_result_boundaries():
    log = (
        _game_log(end_turn=10, winner_seat=1, winner_name="A")
        + _game_log(end_turn=14, winner_seat=2, winner_name="B")
        + _game_log(end_turn=8,  winner_seat=1, winner_name="A")
    )
    ma = analyze(log)
    assert ma.total_games == 3
    winners = [g.winner_name for g in ma.games]
    assert winners == ["A", "B", "A"]
    # Aggregates
    summary = ma.per_deck_summary()
    assert summary["A"]["wins"] == 2
    assert summary["B"]["wins"] == 1


def test_avg_turns_and_duration():
    log = (
        _game_log(end_turn=10, winner_seat=1, winner_name="A", duration_ms=50000)
        + _game_log(end_turn=20, winner_seat=2, winner_name="B", duration_ms=100000)
    )
    ma = analyze(log)
    assert ma.avg_turns == 15.0
    assert ma.avg_duration_sec == 75.0


def test_confirm_action_attributed_per_game():
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "default implementation of confirmAction is used by Foo\n"
        "default implementation of confirmAction is used by Bar\n"
        "Game Outcome: Turn 5\n"
        "Game Result: Game 1 ended in 30000 ms. Ai(1)-A has won!\n"
        "Turn: Turn 1 (Ai(2)-B)\n"
        "default implementation of confirmAction is used by Baz\n"
        "Game Outcome: Turn 7\n"
        "Game Result: Game 2 ended in 40000 ms. Ai(2)-B has won!\n"
    )
    ma = analyze(log)
    assert ma.games[0].confirm_action_count == 2
    assert ma.games[1].confirm_action_count == 1


def test_per_deck_summary_tracks_eliminations_and_fastest():
    # Two games where deck B is eliminated at turn 5 (game 1), then turn 10
    # (game 2). Turn markers advance to those values BEFORE the killing Life
    # line — that's the chronology a real Forge log produces.
    log = (
        "Turn: Turn 1 (Ai(2)-B)\n"
        "Turn: Turn 5 (Ai(2)-B)\n"
        "Life: Life: Ai(2)-B 40 > 0\n"
        "Game Outcome: Turn 5\n"
        "Game Result: Game 1 ended in 30000 ms. Ai(1)-A has won!\n"
        "Turn: Turn 1 (Ai(2)-B)\n"
        "Turn: Turn 10 (Ai(2)-B)\n"
        "Life: Life: Ai(2)-B 40 > 0\n"
        "Game Outcome: Turn 10\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(1)-A has won!\n"
    )
    ma = analyze(log)
    summary = ma.per_deck_summary()
    assert summary["B"]["eliminations"] == 2
    assert summary["B"]["fastest_elimination_turn"] == 5


def test_two_game_match_smoke():
    """End-to-end sanity on a 2-game match where one deck sweeps.

    Replaces the old ``test_real_test2_rerun_smoke``, which read a
    captured ``test2_rerun_v2.json`` that was never committed and so
    skipped forever (silent zero coverage). This committed inline log
    exercises the same path — multi-game parse + per-deck win tally —
    without the external fixture.
    """
    log = (
        # Game 1 — First Sliver Fun (seat 1) beats Goblin Rush (seat 2).
        "Turn: Turn 1 (Ai(1)-First Sliver Fun)\n"
        "Life: Life: Ai(1)-First Sliver Fun 40 > 38\n"
        "Life: Life: Ai(2)-Goblin Rush 40 > 0\n"
        "Game Outcome: Turn 8\n"
        "Game Outcome: Ai(1)-First Sliver Fun has won\n"
        "Game Result: Game 1 ended in 48000 ms. Ai(1)-First Sliver Fun has won!\n"
        # Game 2 — First Sliver Fun sweeps.
        "Turn: Turn 1 (Ai(1)-First Sliver Fun)\n"
        "Life: Life: Ai(2)-Goblin Rush 40 > 0\n"
        "Game Outcome: Turn 9\n"
        "Game Outcome: Ai(1)-First Sliver Fun has won\n"
        "Game Result: Game 2 ended in 54000 ms. Ai(1)-First Sliver Fun has won!\n"
    )
    ma = analyze(log)
    assert ma.total_games == 2
    assert ma.draws == 0
    summary = ma.per_deck_summary()
    assert summary["First Sliver Fun"]["wins"] == 2


def test_to_dict_is_json_serializable():
    log = _game_log(end_turn=10, winner_seat=1, winner_name="A")
    ma = analyze(log)
    # Should round-trip via JSON without a custom encoder.
    s = ma.to_json()
    parsed = json.loads(s)
    assert parsed["total_games"] == 1


# --- Draw -> life/board leader resolution (operator policy point 1) ---------


def test_decisive_game_resolved_winner_matches_winner_seat():
    """For a normal decisive game, resolved_winner_seat just mirrors
    winner_seat and resolved_is_draw is False."""
    log = _game_log(end_turn=12, winner_seat=1, winner_name="Foo Deck")
    g = analyze(log).games[0]
    assert g.is_draw is False
    assert g.resolved_winner_seat == 1
    assert g.resolved_winner_name == "Foo Deck"
    assert g.resolved_is_draw is False


def test_turn_cap_draw_resolves_to_highest_life_seat():
    """A turn-cap draw with a STRICTLY-highest ending_life seat resolves a
    winner = that seat. is_draw/winner_seat stay untouched (backward compat)."""
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Life: Life: Ai(1)-A 40 > 28\n"
        "Life: Life: Ai(2)-B 40 > 15\n"
        "Life: Life: Ai(3)-C 40 > 9\n"
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"  # no "has won!" clause
    )
    g = analyze(log).games[0]
    # Backward-compat: the raw draw signal is preserved.
    assert g.is_draw is True
    assert g.winner_seat is None
    assert g.winner_name is None
    # New policy: unique life leader (seat 1 @ 28) is the resolved winner.
    assert g.resolved_winner_seat == 1
    assert g.resolved_winner_name == "A"
    assert g.resolved_is_draw is False


def test_turn_cap_draw_with_tied_top_life_stays_a_draw():
    """A turn-cap draw with NO unique maximum (tie at the top) stays a real
    draw: resolved_winner_seat is None."""
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Life: Life: Ai(1)-A 40 > 22\n"
        "Life: Life: Ai(2)-B 40 > 22\n"  # tie with seat 1 at the top
        "Life: Life: Ai(3)-C 40 > 5\n"
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"
    )
    g = analyze(log).games[0]
    assert g.is_draw is True
    assert g.resolved_winner_seat is None
    assert g.resolved_winner_name is None
    assert g.resolved_is_draw is True


def test_turn_cap_draw_does_not_crown_eliminated_seat():
    """If the highest-ending_life seat was ELIMINATED (life hit <= 0), it must
    not be crowned the draw winner. Here all three seats died (life <= 0), so
    there's no valid living leader and the game stays a real draw — previously
    seat A (ending_life 0, the max) was wrongly resolved as the winner."""
    log = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Life: Life: Ai(1)-A 40 > 0\n"     # eliminated (after <= 0), but the max
        "Life: Life: Ai(2)-B 40 > -5\n"    # eliminated
        "Life: Life: Ai(3)-C 40 > -10\n"   # eliminated
        "Stopping slow match as draw\n"
        "Game Outcome: Turn 50\n"
        "Game Result: Game 1 ended in 240000 ms\n"
    )
    g = analyze(log).games[0]
    assert g.is_draw is True
    assert g.resolved_winner_seat is None   # eliminated seat A not crowned
    assert g.resolved_is_draw is True
