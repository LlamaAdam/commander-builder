"""Game-level analyzer that extracts richer per-game telemetry from Forge stdout.

`log_parser.py` is the authoritative source of match-level outcomes (wins, total
games, raw event counts). This module sits on top of it and builds per-game
narratives — turn counts, life curves, win source, draws — that the curator
and an eventual analyst layer use to answer questions like:

  - "Does deck X tend to win fast or grind out?"
  - "Which decks lose to Combat damage vs Mill vs Commander damage?"
  - "Did the AI struggle (confirmAction) cluster in early or late game?"

The signals come from Forge log lines that `log_parser` deliberately ignores
(it's the matcher; this is the analyst). Findings:

  Turn: Turn N (Ai(X)-DeckName)
      Turn boundary with the active player. Used for cadence and elimination
      timing. Each game starts at Turn 1.

  Game Outcome: Turn N
      Single line BEFORE the 4-player "won" attribution bug. Tells us the turn
      at which the game ended. Authoritative.

  Game Result: Game N ended in Xms. Ai(N)-DeckName has won!
      The trailing `has won!` clause is the *correct* winner — unlike the four
      Game Outcome lines that all say "won" in 4-player commander. Use this.

  Game Outcome: Ai(N)-DeckName has lost <reason>
      Per-seat loss attribution, emitted in a block at game end (after the
      last Turn line, right before Game Result). Verified against real
      headless logs (vendor/forge*/userdata/forge*.log): losers ALWAYS get a
      "has lost <reason>" line, even when they were eliminated many turns
      before the game ended. The sibling "has won because all opponents have
      lost" lines are NOT reliable — real logs show up to all four seats
      marked "has won" in one game — but the "has lost" lines are: a seat
      with one genuinely left the game. Forge's en-US.properties enumerates
      every loss phrasing it can emit:
        has lost because life total reached 0
        has lost due to accumulation of 21 damage from generals  (commander)
        has lost because of obtaining 10 poison counters
        has lost trying to draw cards from empty library         (mill-out)
        has lost due to effect of spell '<name>'
        has lost because an opponent has won by spell '<name>'
        has lost for unknown reason (this is a bug)
      Commander damage / poison / mill / spell losses happen at POSITIVE
      life, so the Life: stream alone cannot see them — elimination MUST
      also be derived from these lines or a dead seat looks alive.

  Stopping slow match as draw
      Forge concedes after its turn-cap. Authoritative draw marker.

  Life: Life: Ai(N)-DeckName M > N
      Per-deck life transitions. Decreases = damage/loss; increases = lifegain.
      Doesn't say WHO dealt the damage, only the target.

This is read-only: the analyzer never mutates ParsedSim. It returns a separate
GameAnalysis object that downstream consumers can persist alongside.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .log_parser import _normalize  # reuse the existing normalizer


# Game N starts at "Turn: Turn 1 (...)" — but Forge logs *all* games' turns
# without an explicit reset marker between games in 4-player commander. We
# detect game boundaries via Game Result lines, which always close out one game,
# and treat everything between two Game Results as one game's events.
_TURN = re.compile(r"^Turn:\s+Turn\s+(\d+)\s+\(Ai\((\d+)\)-(.+?)\)\s*$")
_GAME_OUTCOME_TURN = re.compile(r"^Game Outcome:\s+Turn\s+(\d+)\s*$")
_GAME_RESULT_WIN = re.compile(
    r"^Game Result:\s*Game\s+(\d+)\s+ended in\s+(\d+)\s*ms\.\s*Ai\((\d+)\)-(.+?)\s+has won!\s*$",
    re.IGNORECASE,
)
_GAME_RESULT_NO_WIN = re.compile(
    r"^Game Result:\s*Game\s+(\d+)\s+ended in\s+(\d+)\s*ms\b",
    re.IGNORECASE,
)
_DRAW = re.compile(r"^Stopping slow match as draw\b", re.IGNORECASE)
# Per-seat loss line from the end-of-game Game Outcome block (see module
# docstring for the full evidence). We match ONLY "has lost" — the "has won"
# variants are known-buggy in 4-player commander (multiple seats get one) and
# stay ignored. All seven loss phrasings share the "has lost" stem; the tail
# is kept as the human-readable reason.
_GAME_OUTCOME_LOST = re.compile(
    r"^Game Outcome:\s+Ai\((\d+)\)-(.+?)\s+has lost\b\s*(.*)$",
    re.IGNORECASE,
)
_LIFE = re.compile(
    r"^Life:\s+Life:\s+Ai\((\d+)\)-(.+?)\s+(\d+)\s*>\s*(-?\d+)\s*$"
)
_CONFIRM_ACTION_LINE = re.compile(
    r"default implementation of confirmAction is used by\s*(.+)", re.IGNORECASE
)


@dataclass
class DeckGameStats:
    seat: int
    name: str
    starting_life: Optional[int] = None
    ending_life: Optional[int] = None
    life_min: Optional[int] = None
    life_max: Optional[int] = None
    damage_taken: int = 0    # sum of life decreases (excludes pay-life)
    life_gained: int = 0     # sum of life increases
    # True when the seat left the game: EITHER a Life: transition to <= 0
    # OR an explicit "Game Outcome: ... has lost <reason>" line. The latter
    # catches commander damage / poison / mill / spell losses that happen at
    # POSITIVE life and are invisible to the Life: stream.
    eliminated: bool = False
    eliminated_turn: Optional[int] = None  # turn at which life first hit 0
    # Tail of the "has lost" outcome line (e.g. "due to accumulation of 21
    # damage from generals"). None when the seat survived or when the only
    # elimination signal was the Life: stream (older logs / truncation).
    loss_reason: Optional[str] = None

    @property
    def normalized_name(self) -> str:
        return _normalize(self.name)


@dataclass
class GameAnalysis:
    game_index: int           # 1-based
    duration_ms: int
    end_turn: Optional[int]
    winner_seat: Optional[int]
    winner_name: Optional[str]
    is_draw: bool
    deck_stats: list[DeckGameStats] = field(default_factory=list)
    confirm_action_count: int = 0  # within this game's window
    # Operator verdict-scoring policy (2026-05): a turn-cap "Stopping slow
    # match as draw" game is no longer scored as a no-result. We resolve a
    # winner = the seat with the STRICTLY-highest ending_life. Decisive games
    # mirror winner_seat/winner_name here. A draw with no unique life leader
    # (tie at the top) stays a real draw -> resolved_winner_seat is None.
    # The raw is_draw / winner_seat / winner_name fields are left UNCHANGED
    # for backward compatibility with existing consumers.
    resolved_winner_seat: Optional[int] = None
    resolved_winner_name: Optional[str] = None

    @property
    def winner_normalized(self) -> Optional[str]:
        return _normalize(self.winner_name) if self.winner_name else None

    @property
    def resolved_winner_normalized(self) -> Optional[str]:
        return _normalize(self.resolved_winner_name) if self.resolved_winner_name else None

    @property
    def resolved_is_draw(self) -> bool:
        """True only when the game has no resolved winner at all. A turn-cap
        draw that resolved to a unique life leader is NOT a draw under the
        operator policy."""
        return self.resolved_winner_seat is None

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0


@dataclass
class MatchAnalysis:
    games: list[GameAnalysis] = field(default_factory=list)

    @property
    def total_games(self) -> int:
        return len(self.games)

    @property
    def draws(self) -> int:
        return sum(1 for g in self.games if g.is_draw)

    @property
    def avg_turns(self) -> float:
        turns = [g.end_turn for g in self.games if g.end_turn is not None]
        return sum(turns) / len(turns) if turns else 0.0

    @property
    def avg_duration_sec(self) -> float:
        if not self.games:
            return 0.0
        return sum(g.duration_sec for g in self.games) / len(self.games)

    def per_deck_summary(self) -> dict[str, dict]:
        """Aggregate per-deck stats across all games. Keyed by normalized name."""
        agg: dict[str, dict] = {}
        for g in self.games:
            for d in g.deck_stats:
                key = d.normalized_name
                row = agg.setdefault(key, {
                    "name": d.name,
                    "games": 0,
                    "wins": 0,
                    "eliminations": 0,
                    "total_damage_taken": 0,
                    "total_life_gained": 0,
                    "avg_ending_life": 0.0,
                    "_ending_life_sum": 0,
                    "_ending_life_n": 0,
                    "fastest_elimination_turn": None,
                })
                row["games"] += 1
                if g.winner_normalized == key:
                    row["wins"] += 1
                if d.eliminated:
                    row["eliminations"] += 1
                    if d.eliminated_turn is not None:
                        cur = row["fastest_elimination_turn"]
                        row["fastest_elimination_turn"] = (
                            d.eliminated_turn if cur is None
                            else min(cur, d.eliminated_turn)
                        )
                row["total_damage_taken"] += d.damage_taken
                row["total_life_gained"] += d.life_gained
                if d.ending_life is not None:
                    row["_ending_life_sum"] += d.ending_life
                    row["_ending_life_n"] += 1
        # Finalize averages and drop scratch fields.
        for row in agg.values():
            n = row.pop("_ending_life_n")
            s = row.pop("_ending_life_sum")
            row["avg_ending_life"] = round(s / n, 1) if n else 0.0
        return agg

    def to_dict(self) -> dict:
        return {
            "total_games": self.total_games,
            "draws": self.draws,
            "avg_turns": round(self.avg_turns, 1),
            "avg_duration_sec": round(self.avg_duration_sec, 1),
            "per_deck_summary": self.per_deck_summary(),
            "games": [asdict(g) for g in self.games],
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), indent=2, **kwargs)


def analyze(stdout: str) -> MatchAnalysis:
    """Walk the Forge log line-by-line and split into per-game analyses.

    State machine:
      - Lines accumulate into `current_game_lines` until a Game Result fires.
      - On Game Result, we summarize the buffered lines into a GameAnalysis,
        then reset the buffer for the next game.
      - Lines before the first turn marker are ignored (boot/init noise).
    """
    if not stdout:
        return MatchAnalysis()

    # Split the stream by Game Result boundaries; each chunk is one game.
    lines = stdout.splitlines()
    games: list[GameAnalysis] = []
    buf: list[str] = []
    started = False  # only collect once we see the first Turn line

    def flush(end_line: str) -> None:
        if not buf:
            return
        ga = _summarize_game(buf, end_line, len(games) + 1)
        if ga is not None:
            games.append(ga)
        buf.clear()

    for line in lines:
        s = line.rstrip()
        if not started:
            if _TURN.match(s):
                started = True
            else:
                continue
        # Game Result terminates the current game's buffer.
        if _GAME_RESULT_WIN.match(s) or _GAME_RESULT_NO_WIN.match(s):
            flush(s)
            started = False  # next game's buffer will start at its own Turn 1
            continue
        buf.append(s)

    # Trailing lines without a closing Game Result get dropped — partial games
    # have no authoritative end signal and would skew aggregates.

    return MatchAnalysis(games=games)


def _summarize_game(lines: list[str], end_line: str, game_index: int) -> Optional[GameAnalysis]:
    """Build a GameAnalysis from one game's buffered lines plus the closing
    Game Result line. Returns None if we can't even find an end marker."""
    end_turn: Optional[int] = None
    duration_ms = 0
    winner_seat: Optional[int] = None
    winner_name: Optional[str] = None

    m_win = _GAME_RESULT_WIN.match(end_line)
    m_any = _GAME_RESULT_NO_WIN.match(end_line)
    if m_win:
        duration_ms = int(m_win.group(2))
        winner_seat = int(m_win.group(3))
        winner_name = m_win.group(4).strip()
    elif m_any:
        duration_ms = int(m_any.group(2))
    else:
        return None

    is_draw = False
    confirm_action_count = 0
    # seat -> DeckGameStats
    decks: dict[int, DeckGameStats] = {}

    for s in lines:
        # Turn marker: also seeds deck identities (seat + name).
        m_turn = _TURN.match(s)
        if m_turn:
            turn_n = int(m_turn.group(1))
            seat = int(m_turn.group(2))
            name = m_turn.group(3).strip()
            d = decks.get(seat)
            if d is None:
                d = DeckGameStats(seat=seat, name=name)
                decks[seat] = d
            # Track the highest turn we observed; Game Outcome will refine.
            if end_turn is None or turn_n > end_turn:
                end_turn = turn_n
            continue

        # Authoritative end-turn marker (overrides our highest-Turn estimate).
        m_outcome = _GAME_OUTCOME_TURN.match(s)
        if m_outcome:
            end_turn = int(m_outcome.group(1))
            continue

        # Per-seat "has lost" outcome line — the ONLY signal for non-life
        # eliminations (commander damage, poison, mill, spell) which end a
        # player at positive life. Must come before the generic fall-through
        # so a dead seat can never masquerade as a living life leader.
        #
        # TURN-CAP-STOP EVIDENCE (verified 2026-07-20 against the real soak
        # logs under vendor/forge*/userdata/forge*.log). The risk under
        # review: if Forge marked EVERY seat "has lost" when it stops a
        # slow match, all seats would become `eliminated` here and
        # _resolve_life_leader would return (None, None) — silently
        # degrading every turn-cap draw to a plain draw. It does NOT.
        # In cap-stopped games (marked "Stopping slow match as draw"),
        # surviving seats get the known-buggy "has won" line, never a
        # "has lost" line. vendor/forge10/userdata/forge.log (game ended
        # at the ~120s cap, "Game 1 ended in 120175 ms"):
        #   Game Outcome: Turn 21
        #   Game Outcome: Ai(1)-Choco, Seeker of Paradise has lost because life total reached 0
        #   Game Outcome: Ai(2)-Eldrazi Incursion [M3C] [2024] has lost because life total reached 0
        #   Game Outcome: Ai(3)-Graveyard Overdrive [M3C] [2024] has won because all opponents have lost
        #   Game Outcome: Ai(4)-Creative Energy [M3C] [2024] has won because all opponents have lost
        # And vendor/forge2/userdata/forge0.log shows the all-survivors
        # cap stop ("Game 1 ended in 120112 ms") with ZERO "has lost"
        # lines — all four seats read "has won because all opponents have
        # lost". Across all 9 cap-stopped games in the corpus the only
        # loss phrasing Forge ever emitted is "has lost because life
        # total reached 0" (24 lines total); no cap-specific loss reason
        # exists in the wild. So a cap stop leaves genuine survivors
        # un-eliminated and the life-leader resolution stays intact.
        m_lost = _GAME_OUTCOME_LOST.match(s)
        if m_lost:
            seat = int(m_lost.group(1))
            name = m_lost.group(2).strip()
            d = decks.setdefault(seat, DeckGameStats(seat=seat, name=name))
            d.eliminated = True
            # Deliberately NOT setting eliminated_turn here: the outcome
            # block is emitted at game END, not at the moment of death, so
            # the current turn context says nothing about WHEN a non-life
            # elimination happened. The Life: stream already stamps the turn
            # for life-based deaths; for the rest it stays None (honest
            # "unknown") rather than a fabricated end-of-game turn.
            d.loss_reason = m_lost.group(3).strip() or None
            continue

        if _DRAW.search(s):
            is_draw = True
            continue

        m_life = _LIFE.match(s)
        if m_life:
            seat = int(m_life.group(1))
            name = m_life.group(2).strip()
            before = int(m_life.group(3))
            after = int(m_life.group(4))
            d = decks.setdefault(seat, DeckGameStats(seat=seat, name=name))
            if d.starting_life is None:
                d.starting_life = before
            d.ending_life = after
            d.life_min = after if d.life_min is None else min(d.life_min, after)
            d.life_max = before if d.life_max is None else max(d.life_max, before)
            delta = after - before
            if delta < 0:
                d.damage_taken += -delta
                if after <= 0 and not d.eliminated:
                    d.eliminated = True
                    d.eliminated_turn = end_turn
            elif delta > 0:
                d.life_gained += delta
            continue

        if _CONFIRM_ACTION_LINE.search(s):
            confirm_action_count += 1
            continue

    # Forge's "all four players won" bug means winner_name from Game Result is
    # the only reliable source. If absent (rare — happens on draws or when the
    # log got truncated), fall back to crowning the SOLE surviving seat.
    # Previously this compared the top-two ending_life values, which had two
    # holes: (1) a seat dead to commander damage / poison / mill at positive
    # life could be crowned, and (2) a seat with NO life data was treated as
    # dead (None -> 0). Now a seat only counts as dead when it is provably
    # eliminated (Life: hit <= 0, or an explicit "has lost" outcome line);
    # anything less certain leaves winner as None — an honest draw beats a
    # fabricated winner.
    if winner_name is None and not is_draw and decks:
        survivors = [d for d in decks.values() if not d.eliminated]
        if len(decks) >= 2 and len(survivors) == 1:
            winner_seat = survivors[0].seat
            winner_name = survivors[0].name

    # Resolve a winner per the operator verdict-scoring policy. For decisive
    # games this just mirrors winner_seat/winner_name. For turn-cap draws we
    # pick the seat with the STRICTLY-highest ending_life; a tie at the top
    # leaves the game a real draw (resolved_winner_seat stays None).
    resolved_winner_seat = winner_seat
    resolved_winner_name = winner_name
    if winner_seat is None and decks:
        resolved_winner_seat, resolved_winner_name = _resolve_life_leader(
            list(decks.values())
        )

    return GameAnalysis(
        game_index=game_index,
        duration_ms=duration_ms,
        end_turn=end_turn,
        winner_seat=winner_seat,
        winner_name=winner_name,
        is_draw=is_draw,
        deck_stats=sorted(decks.values(), key=lambda d: d.seat),
        confirm_action_count=confirm_action_count,
        resolved_winner_seat=resolved_winner_seat,
        resolved_winner_name=resolved_winner_name,
    )


def _resolve_life_leader(
    decks: list[DeckGameStats],
) -> tuple[Optional[int], Optional[str]]:
    """Return the (seat, name) of the deck with the STRICTLY-highest
    ending_life, or (None, None) if there's no unique maximum (tie at the
    top) or no usable life data. Decks with an unknown ending_life are
    excluded from contention. Eliminated seats are also excluded — a player
    who already lost (life <= 0, or commander-damage/poison/mill while still
    at positive life) must never be crowned the winner of a turn-cap draw.
    Non-life eliminations are visible because _summarize_game parses the
    per-seat "Game Outcome: ... has lost <reason>" lines, so `eliminated` is
    trustworthy here regardless of the seat's ending life total."""
    scored = [d for d in decks
              if d.ending_life is not None and not d.eliminated]
    if not scored:
        return None, None
    top = max(d.ending_life for d in scored)  # type: ignore[type-var]
    leaders = [d for d in scored if d.ending_life == top]
    if len(leaders) != 1:
        return None, None  # tie at the top -> stays a real draw
    return leaders[0].seat, leaders[0].name


if __name__ == "__main__":
    # Smoke entry: pipe a SimResult JSON in via stdin or pass a path.
    import sys
    if len(sys.argv) < 2:
        print("Usage: game_analyzer.py <sim_result.json>")
        raise SystemExit(2)
    with open(sys.argv[1], encoding="utf-8") as _fh:
        payload = json.loads(_fh.read())
    stdout = payload.get("stdout", "")
    ma = analyze(stdout)
    print(ma.to_json())
