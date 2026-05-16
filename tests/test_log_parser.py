"""Pin the log_parser surface that the curator depends on.

Focuses on the contracts that broke us in Phase 1A/1B:
  - Match Result splitting on "Ai(N)-" (deck names with colons).
  - _normalize stripping [USER]/[Bn]/.dck across the three name variants.
  - draws inferred as games_completed - sum(wins).
  - win_rate excluding draws from the denominator.
"""
from commander_builder.log_parser import (
    DeckResult,
    ParsedSim,
    _normalize,
    parse,
)


def test_normalize_strips_user_prefix_and_bracket_suffix():
    assert _normalize("[USER] Foo Deck [B3].dck") == "Foo Deck"
    assert _normalize("Foo Deck [B5]") == "Foo Deck"
    assert _normalize("Foo Deck.dck") == "Foo Deck"
    assert _normalize("[USER] Foo Deck") == "Foo Deck"
    assert _normalize("Foo Deck") == "Foo Deck"


def test_normalize_handles_question_mark_bracket():
    # "[B?]" appears on imports with unknown bracket.
    assert _normalize("Foo Deck [B?].dck") == "Foo Deck"


def test_parse_match_result_with_colons_in_name():
    stdout = (
        "Match Result: Ai(1)-Kinnan: Primer A: 2 "
        "Ai(2)-Bone Daddy: 1 Ai(3)-C: 0 Ai(4)-D: 0 "
    )
    parsed = parse(stdout)
    names = [d.name for d in parsed.deck_results]
    wins = [d.wins for d in parsed.deck_results]
    assert names == ["Kinnan: Primer A", "Bone Daddy", "C", "D"]
    assert wins == [2, 1, 0, 0]


def test_games_completed_counts_game_result_lines():
    stdout = (
        "Match Result: Ai(1)-A: 1 Ai(2)-B: 0 Ai(3)-C: 0 Ai(4)-D: 0\n"
        "Game Result: Game 1 ended in 60000 ms\n"
        "Game Result: Game 2 ended in 80000 ms\n"
    )
    parsed = parse(stdout)
    assert parsed.games_completed == 2
    assert parsed.total_game_ms == 140000


def test_games_completed_backfills_from_wins_when_no_game_result():
    # Some Forge builds quiet Game Result lines; we backfill from sum(wins).
    stdout = "Match Result: Ai(1)-A: 2 Ai(2)-B: 1 Ai(3)-C: 0 Ai(4)-D: 0"
    parsed = parse(stdout)
    assert parsed.games_completed == 3


def test_draws_inferred_from_completed_minus_wins():
    parsed = ParsedSim(
        deck_results=[
            DeckResult(seat=1, name="A", wins=1),
            DeckResult(seat=2, name="B", wins=0),
            DeckResult(seat=3, name="C", wins=0),
            DeckResult(seat=4, name="D", wins=0),
        ],
        games_completed=3,
    )
    assert parsed.draws == 2


def test_win_rate_excludes_draws_from_denominator():
    parsed = ParsedSim(
        deck_results=[
            DeckResult(seat=1, name="A", wins=2),
            DeckResult(seat=2, name="B", wins=0),
            DeckResult(seat=3, name="C", wins=0),
            DeckResult(seat=4, name="D", wins=0),
        ],
        games_completed=4,  # 2 draws
    )
    # Decisive games = 4 - 2 = 2; A won both => 1.0, not 2/4=0.5
    assert parsed.win_rate("A") == 1.0
    assert parsed.win_rate("B") == 0.0


def test_win_rate_matches_against_decorated_or_normalized_name():
    parsed = ParsedSim(
        deck_results=[DeckResult(seat=1, name="Foo Deck", wins=1)],
        games_completed=1,
    )
    assert parsed.win_rate("Foo Deck") == 1.0
    assert parsed.win_rate("[USER] Foo Deck [B3].dck") == 1.0
    assert parsed.win_rate("Foo Deck [B5]") == 1.0


def test_unsupported_and_confirm_action_collected():
    stdout = (
        "Match Result: Ai(1)-A: 1 Ai(2)-B: 0 Ai(3)-C: 0 Ai(4)-D: 0\n"
        "An unsupported card was requested: Frobnicate the Bear\n"
        "default implementation of confirmAction is used by Murky Choice\n"
        "default implementation of confirmAction is used by Murky Choice\n"
        "Game Result: Game 1 ended in 50000 ms\n"
    )
    parsed = parse(stdout)
    assert parsed.unsupported_cards == ["Frobnicate the Bear"]
    assert parsed.confirm_action_cards == ["Murky Choice", "Murky Choice"]
    assert parsed.confirm_action_per_game == 2.0


def test_empty_stdout_returns_empty_parse():
    parsed = parse("")
    assert parsed.deck_results == []
    assert parsed.games_completed == 0
    assert parsed.draws == 0
    assert parsed.win_rate("anything") is None


def test_confirm_action_attributed_to_active_player():
    """Phase markers tell us who the active player is. confirmAction events
    in between attribute to that player. Replaces the prior even-pod-split
    stopgap."""
    stdout = (
        "Match Result: Ai(1)-A: 1 Ai(2)-B: 0 Ai(3)-C: 0 Ai(4)-D: 0\n"
        "Phase: Ai(1)-A Untap\n"
        "default implementation of confirmAction is used by Card1\n"
        "default implementation of confirmAction is used by Card2\n"
        "Phase: Ai(2)-B Untap\n"
        "default implementation of confirmAction is used by Card3\n"
        "Game Result: Game 1 ended in 50000 ms\n"
    )
    parsed = parse(stdout)
    assert len(parsed.confirm_action_cards) == 3
    assert parsed.confirm_action_by_deck == {"A": 2, "B": 1}


def test_confirm_action_attribution_falls_back_when_no_phase_marker():
    """Older Forge builds may omit Phase lines. The total count still tracks;
    the per-deck dict just stays empty so callers can detect the fallback."""
    stdout = (
        "Match Result: Ai(1)-A: 1 Ai(2)-B: 0 Ai(3)-C: 0 Ai(4)-D: 0\n"
        "default implementation of confirmAction is used by Card1\n"
        "Game Result: Game 1 ended in 50000 ms\n"
    )
    parsed = parse(stdout)
    assert parsed.confirm_action_cards == ["Card1"]
    assert parsed.confirm_action_by_deck == {}


def test_confirm_action_attribution_uses_normalized_names():
    """The Phase line includes the deck's internal Name= which may have
    decoration ([B?] etc.). Normalization ensures lookup works against
    filenames or raw match-result names interchangeably."""
    stdout = (
        "Phase: Ai(1)-Foo Deck Untap\n"
        "default implementation of confirmAction is used by Card1\n"
        "Game Result: Game 1 ended in 50000 ms\n"
    )
    parsed = parse(stdout)
    # Looked up via normalized form.
    assert parsed.confirm_action_by_deck.get("Foo Deck") == 1


def test_parse_uses_last_match_result_for_constructed_format():
    """Forge's ``constructed`` format emits a Match Result line per
    game with cumulative win counts; the LAST line is the final tally.
    Forge's ``commander`` format emits one Match Result at the very
    end. Both should parse correctly with "use last" semantics."""
    # Synthetic constructed-format stdout: 3 games, cumulative tallies.
    stdout = (
        "Game Result: Game 1 ended in 5000 ms. Ai(2)-Bar has won!\n"
        "Match Result: Ai(1)-Foo: 0 Ai(2)-Bar: 1\n"
        "Game Result: Game 2 ended in 5000 ms. Ai(1)-Foo has won!\n"
        "Match Result: Ai(1)-Foo: 1 Ai(2)-Bar: 1\n"
        "Game Result: Game 3 ended in 5000 ms. Ai(1)-Foo has won!\n"
        "Match Result: Ai(1)-Foo: 2 Ai(2)-Bar: 1\n"
    )
    parsed = parse(stdout)
    # The LAST Match Result line is the final tally (Foo 2, Bar 1),
    # not the first (Foo 0, Bar 1).
    foo = next((d for d in parsed.deck_results if d.name == "Foo"), None)
    bar = next((d for d in parsed.deck_results if d.name == "Bar"), None)
    assert foo is not None
    assert bar is not None
    assert foo.wins == 2
    assert bar.wins == 1


def test_parse_commander_format_unchanged_with_use_last_fix():
    """One Match Result line at the end of all games — typical
    commander format output. The "use last" change must not regress
    this case."""
    stdout = (
        "Game Result: Game 1 ended in 80000 ms. Ai(3)-Allies has won!\n"
        "Game Result: Game 2 ended in 90000 ms. Ai(2)-Hash has won!\n"
        "Game Result: Game 3 ended in 70000 ms. Ai(3)-Allies has won!\n"
        "Match Result: Ai(1)-Hakbal: 0 Ai(2)-Hash: 1 Ai(3)-Allies: 2 Ai(4)-Yuriko: 0\n"
    )
    parsed = parse(stdout)
    by_name = {d.name: d.wins for d in parsed.deck_results}
    assert by_name == {
        "Hakbal": 0, "Hash": 1, "Allies": 2, "Yuriko": 0,
    }
