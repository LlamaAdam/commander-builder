"""FP-016 replay-lite — tests for the pure log→timeline parser.

Fixture-driven: the logs under ``tests/fixtures/replays/`` are synthesized
from the SAME line vocabulary log_parser / game_analyzer verified against
real headless captures (Turn:, Phase:, Life:, Game Outcome loss reasons,
Stopping slow match as draw, Game Result, confirmAction, unsupported
card). Covers:

- split_games: multi-game splitting at Game Result boundaries, trailing
  partial chunks, boot-noise-only tails dropped.
- parse_timeline on a complete game: players, turns, active players,
  life events, life-total snapshots, eliminations (life-based AND
  commander-damage-at-positive-life), winner, end turn, duration.
- parse_timeline on truncated logs: honest partial timeline with
  truncated=True and no fabricated winner.
- draw games, best-effort cast/attack lines, empty/garbage input.
"""
from __future__ import annotations

from pathlib import Path

from commander_builder.replay_timeline import parse_timeline, split_games

FIXTURES = Path(__file__).parent / "fixtures" / "replays"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# split_games
# ---------------------------------------------------------------------------

def test_split_single_complete_game():
    chunks = split_games(_read("complete_game.log"))
    assert len(chunks) == 1
    assert chunks[0]["complete"] is True
    # Boot noise stays attached to game 1's chunk.
    assert "Loading decks" in chunks[0]["text"]
    assert "Game Result:" in chunks[0]["text"]


def test_split_multi_game_with_trailing_partial():
    chunks = split_games(_read("multi_game.log"))
    assert len(chunks) == 3
    assert [c["complete"] for c in chunks] == [True, True, False]
    # Each complete chunk ends with its own Game Result line.
    assert chunks[0]["text"].rstrip().endswith(
        "Game Result: Game 1 ended in 41022 ms. Ai(1)-Alpha Ramp [B3] has won!"
    )
    assert "Game 2 ended in 120175 ms" in chunks[1]["text"]
    # The partial chunk holds the hung game's turns.
    assert "Turn: Turn 2 (Ai(4)-Delta Combo [B3])" in chunks[2]["text"]


def test_split_truncated_only_log_kept_as_partial():
    chunks = split_games(_read("truncated_game.log"))
    assert len(chunks) == 1
    assert chunks[0]["complete"] is False


def test_split_drops_noise_only_tail():
    stdout = (
        "Turn: Turn 1 (Ai(1)-A)\n"
        "Game Result: Game 1 ended in 1000 ms. Ai(1)-A has won!\n"
        "Match Result: Ai(1)-A: 1\n"
        "some shutdown noise\n"
    )
    chunks = split_games(stdout)
    # The trailing Match Result / noise has no Turn line -> not a game.
    assert len(chunks) == 1
    assert chunks[0]["complete"] is True


def test_split_empty_input():
    assert split_games("") == []
    assert split_games(None or "") == []


# ---------------------------------------------------------------------------
# parse_timeline — complete game fixture
# ---------------------------------------------------------------------------

def test_complete_game_players_and_winner():
    tl = parse_timeline(_read("complete_game.log"))
    assert tl["truncated"] is False
    assert [p["seat"] for p in tl["players"]] == [1, 2, 3, 4]
    assert tl["result"]["winner_seat"] == 1
    assert tl["result"]["winner_name"] == "Alpha Ramp [B3]"
    assert tl["result"]["end_turn"] == 8  # authoritative Game Outcome: Turn 8
    assert tl["result"]["duration_ms"] == 95321
    assert tl["result"]["is_draw"] is False


def test_complete_game_turns_and_active_players():
    tl = parse_timeline(_read("complete_game.log"))
    turns = tl["turns"]
    assert [t["turn"] for t in turns] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert turns[0]["active"] == "Alpha Ramp [B3]"
    assert turns[0]["seat"] == 1
    assert turns[3]["active"] == "Delta Combo [B3]"
    assert turns[3]["seat"] == 4


def test_complete_game_life_events_and_snapshots():
    tl = parse_timeline(_read("complete_game.log"))
    turns = {t["turn"]: t for t in tl["turns"]}
    # Turn 2: Beta takes 2 -> life event 40 > 38.
    life_events = [e for e in turns[2]["events"] if e["type"] == "life"]
    assert life_events == [{
        "type": "life", "seat": 2, "name": "Beta Control [B3]",
        "from": 40, "to": 38,
    }]
    # Per-turn life snapshot carries the totals as-of end of that turn
    # (string seat keys for JSON friendliness).
    assert turns[4]["life_totals"]["4"] == 21
    assert turns[4]["life_totals"]["2"] == 40  # lifegain back to 40
    assert turns[6]["life_totals"]["2"] == 12


def test_complete_game_life_elimination_lands_in_turn():
    tl = parse_timeline(_read("complete_game.log"))
    turns = {t["turn"]: t for t in tl["turns"]}
    elims_t5 = [e for e in turns[5]["events"] if e["type"] == "elimination"]
    assert len(elims_t5) == 1
    assert elims_t5[0]["seat"] == 4
    # The end-of-game outcome block refines the inferred reason to
    # Forge's explicit phrasing.
    delta = next(p for p in tl["players"] if p["seat"] == 4)
    assert delta["eliminated"] is True
    assert delta["loss_reason"] == "because life total reached 0"


def test_complete_game_non_life_eliminations_captured():
    """Commander damage + mill-out kill at POSITIVE life — invisible to
    the Life: stream, only the outcome block knows. Both must be
    eliminations with their reasons, and NO duplicate elimination event
    may exist for the life-based death."""
    tl = parse_timeline(_read("complete_game.log"))
    elims = tl["result"]["eliminations"]
    by_seat = {e["seat"]: e for e in elims}
    assert set(by_seat) == {2, 3, 4}
    assert by_seat[2]["reason"] == (
        "due to accumulation of 21 damage from generals"
    )
    assert by_seat[3]["reason"] == (
        "trying to draw cards from empty library"
    )
    # Exactly one elimination EVENT per dead seat across all turns.
    all_events = [e for t in tl["turns"] for e in t["events"]
                  if e["type"] == "elimination"]
    assert sorted(e["seat"] for e in all_events) == [2, 3, 4]
    # Beta died at 12 life — the timeline must not pretend otherwise.
    beta = next(p for p in tl["players"] if p["seat"] == 2)
    assert beta["ending_life"] == 12
    assert beta["eliminated"] is True


def test_complete_game_notable_markers():
    tl = parse_timeline(_read("complete_game.log"))
    # Pre-game unsupported card lands in pregame_events, not a turn.
    assert {"type": "unsupported_card",
            "card": "Obscure Legend of Kamigawa"} in tl["pregame_events"]
    turns = {t["turn"]: t for t in tl["turns"]}
    assert {"type": "confirm_action",
            "card": "Rhystic Study"} in turns[3]["events"]


# ---------------------------------------------------------------------------
# parse_timeline — truncated / draw fixtures
# ---------------------------------------------------------------------------

def test_truncated_log_gets_honest_partial_timeline():
    tl = parse_timeline(_read("truncated_game.log"))
    assert tl["truncated"] is True
    assert tl["result"]["winner_seat"] is None
    assert tl["result"]["winner_name"] is None
    assert tl["result"]["duration_ms"] is None
    # The turns that WERE logged are all present.
    assert [t["turn"] for t in tl["turns"]] == [1, 2, 3]
    # end_turn falls back to the highest observed Turn line.
    assert tl["result"]["end_turn"] == 3


def test_turn_cap_draw_game():
    chunks = split_games(_read("multi_game.log"))
    tl = parse_timeline(chunks[1]["text"])
    assert tl["truncated"] is False  # it DID close with a Game Result
    assert tl["result"]["is_draw"] is True
    assert tl["result"]["winner_seat"] is None  # no "has won!" clause
    assert tl["result"]["end_turn"] == 21
    assert tl["result"]["duration_ms"] == 120175
    # The known-buggy "has won because all opponents have lost" lines
    # must NOT create winners or eliminations.
    assert tl["result"]["eliminations"] == []


def test_partial_third_game_of_multi_log():
    chunks = split_games(_read("multi_game.log"))
    tl = parse_timeline(chunks[2]["text"])
    assert tl["truncated"] is True
    assert [t["turn"] for t in tl["turns"]] == [1, 2]
    assert tl["result"]["winner_name"] is None


# ---------------------------------------------------------------------------
# parse_timeline — edge cases + best-effort event lines
# ---------------------------------------------------------------------------

def test_empty_and_garbage_input():
    tl = parse_timeline("")
    assert tl["turns"] == []
    assert tl["players"] == []
    assert tl["truncated"] is True
    tl2 = parse_timeline("complete nonsense\nnothing forge-like here\n")
    assert tl2["turns"] == []
    assert tl2["truncated"] is True


def test_best_effort_cast_and_attack_lines():
    stdout = (
        "Turn: Turn 1 (Ai(1)-Alpha)\n"
        "Ai(1)-Alpha casts Sol Ring\n"
        "Ai(1)-Alpha attacks Beta with 2 creatures\n"
        "Game Result: Game 1 ended in 5000 ms. Ai(1)-Alpha has won!\n"
    )
    tl = parse_timeline(stdout)
    events = tl["turns"][0]["events"]
    assert {"type": "cast", "seat": 1, "name": "Alpha",
            "spell": "Sol Ring"} in events
    attack = [e for e in events if e["type"] == "attack"]
    assert len(attack) == 1
    assert attack[0]["seat"] == 1


def test_negative_life_transition_counts_as_elimination():
    stdout = (
        "Turn: Turn 4 (Ai(2)-Beta)\n"
        "Life: Life: Ai(1)-Alpha 5 > -3\n"
    )
    tl = parse_timeline(stdout)
    alpha = next(p for p in tl["players"] if p["seat"] == 1)
    assert alpha["eliminated"] is True
    assert alpha["ending_life"] == -3


def test_timeline_is_json_serializable():
    import json
    for name in ("complete_game.log", "truncated_game.log", "multi_game.log"):
        for chunk in split_games(_read(name)):
            json.dumps(parse_timeline(chunk["text"]))


# ---------------------------------------------------------------------------
# Turn-count convention: rounds vs player turns
# ---------------------------------------------------------------------------
#
# The Replays UI showed "ended turn 12" directly above a timeline listing
# turns 1..22 with nothing to say why. Two different counters:
# ``Game Outcome: Turn N`` is the ROUND counter, while each ``Turn:`` line
# is one PLAYER turn. The parser now emits both under unambiguous names
# and leaves the overloaded ``end_turn`` alone for existing consumers.

def _pod_log(player_turns: int, outcome_round: int) -> str:
    """Synthetic pod log: ``player_turns`` Turn: lines closed by a
    Game Outcome round number that deliberately disagrees with them."""
    seats = ["Alpha Ramp [B3]", "Beta Control [B3]"]
    lines = [
        "Turn: Turn %d (Ai(%d)-%s)" % (n, (n - 1) % 2 + 1, seats[(n - 1) % 2])
        for n in range(1, player_turns + 1)
    ]
    lines.append("Game Outcome: Turn %d" % outcome_round)
    lines.append(
        "Game Result: Game 1 ended in 90000 ms. Ai(1)-Alpha Ramp [B3] has won!"
    )
    return "\n".join(lines) + "\n"


def test_round_and_player_turn_counters_are_separate():
    tl = parse_timeline(_pod_log(player_turns=22, outcome_round=12))
    result = tl["result"]
    assert result["end_round"] == 12          # Game Outcome line
    assert result["player_turns"] == 22       # one per Turn: line
    assert len(tl["turns"]) == 22
    # Legacy field keeps its old meaning (outcome line wins) so the
    # replay index and /api/replays consumers are untouched.
    assert result["end_turn"] == 12


def test_end_round_is_none_without_an_outcome_line():
    """A truncated log has no authoritative round number. end_round must
    stay None rather than silently borrowing the max Turn: value the way
    the legacy end_turn fallback does — otherwise the UI would print a
    fabricated round count."""
    tl = parse_timeline(_read("truncated_game.log"))
    assert tl["result"]["end_round"] is None
    assert tl["result"]["player_turns"] == 3
    assert tl["result"]["end_turn"] == 3      # legacy max-Turn fallback


def test_counters_agree_when_the_log_agrees():
    """The fixture pod log numbers its Turn: lines the same way its
    outcome line does, so both counters land on 8 — the labels stay
    unambiguous even when the numbers match."""
    tl = parse_timeline(_read("complete_game.log"))
    assert tl["result"]["end_round"] == 8
    assert tl["result"]["player_turns"] == 8
