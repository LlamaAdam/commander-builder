"""Parse Forge `sim` stdout into structured results.

Phase 1A captured the actual log format. The authoritative anchors:

  Match Result: Ai(N)-<deck>: <wins> ...   ← per-deck match record (use this)
  Game Result: Game N ended in <ms>         ← per-game wall time

Bugs to ignore:
  Game Outcome: ...                          ← 4-player attribution is broken;
                                                often marks all 4 as "won".

Quality signals (count and surface; not failures by themselves):
  An unsupported card was requested:         ← Forge DB gap
  default implementation of confirmAction is used by <card>
                                             ← AI struggle marker

This module returns plain dicts, not opinions. Callers (curator, run_match,
analyst) decide what to do with the numbers.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# Match Result lines vary slightly by version; the wins counter is the bit we
# care about. Examples seen in Phase 1A captures:
#   "Match Result: Ai(1)-DeckName: 2 Ai(2)-OtherDeck: 1"
#   "Match Result: Ai(1)-A: 1 Ai(2)-B: 0 Ai(3)-C: 0 Ai(4)-D: 2"
# Parsing: split the payload on "Ai(N)-" boundaries first, then peel the
# trailing ": <wins>" off each chunk. Splitting first makes the parse robust
# to colons inside deck names ("Kinnan Midrange Control: With Primer").
_MATCH_LINE = re.compile(r"^Match Result:\s*(.+)$")
_AI_SPLIT = re.compile(r"\s*Ai\((\d+)\)-")
_TRAILING_WINS = re.compile(r"^(.*?):\s*(\d+)\s*$", re.DOTALL)

_GAME_END = re.compile(r"^Game Result:.*ended in\s+(\d+)\s*ms", re.IGNORECASE)

_UNSUPPORTED = re.compile(r"An unsupported card was requested:\s*(.+)", re.IGNORECASE)
_CONFIRM_ACTION = re.compile(
    r"default implementation of confirmAction is used by\s*(.+)", re.IGNORECASE
)
# Phase: Ai(N)-DeckName <PhaseType>  → tells us who the active player is.
# Used to attribute confirmAction events to a specific deck instead of dividing
# evenly across the pod (the prior stopgap in pool_curator).
_PHASE = re.compile(r"^Phase:\s+Ai\((\d+)\)-(.+?)\s+\w+\s*$")


def _normalize(name: str) -> str:
    """Strip [USER] prefix and [Bn] suffix so result names match query names
    regardless of whether the caller passes a filename or the deck's internal
    Name= field.

    Order matters: filenames look like `[USER] Foo [B3].dck`, so we strip the
    `.dck` extension before the `[B<n>]` suffix — otherwise the `$` anchor on
    the bracket regex never matches and the bracket remains in the output."""
    s = name.strip()
    s = re.sub(r"^\[USER\]\s*", "", s)
    s = re.sub(r"\.dck$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\[B[0-9?]\]$", "", s)
    return s.strip()


@dataclass
class DeckResult:
    seat: int
    name: str
    wins: int

    @property
    def normalized_name(self) -> str:
        return _normalize(self.name)


@dataclass
class ParsedSim:
    deck_results: list[DeckResult] = field(default_factory=list)
    games_completed: int = 0
    total_game_ms: int = 0
    unsupported_cards: list[str] = field(default_factory=list)
    confirm_action_cards: list[str] = field(default_factory=list)
    raw_match_line: Optional[str] = None
    # confirmAction events attributed to the deck whose Phase line was most
    # recent when the event fired. Keyed by *normalized* deck name (so callers
    # can look up by filename or internal Name=). May be empty if Forge omitted
    # Phase markers. The total here matches len(confirm_action_cards).
    confirm_action_by_deck: dict[str, int] = field(default_factory=dict)

    @property
    def avg_game_sec(self) -> float:
        if not self.games_completed:
            return 0.0
        return (self.total_game_ms / self.games_completed) / 1000.0

    @property
    def confirm_action_per_game(self) -> float:
        if not self.games_completed:
            return 0.0
        return len(self.confirm_action_cards) / self.games_completed

    @property
    def draws(self) -> int:
        """Games that ended with no winner attributed (turn limit, mutual
        elimination). Inferred from games_completed minus total wins —
        Forge doesn't print a draw line we can rely on."""
        if not self.deck_results or not self.games_completed:
            return 0
        return max(0, self.games_completed - sum(d.wins for d in self.deck_results))

    def winner(self) -> Optional[DeckResult]:
        if not self.deck_results:
            return None
        return max(self.deck_results, key=lambda d: d.wins)

    def win_rate(self, deck_name: str) -> Optional[float]:
        """Win rate for `deck_name` (wins / decisive_games). Draws excluded
        from the denominator — counting draws as losses penalizes control
        decks unfairly. Match against raw or normalized name on either side."""
        target = _normalize(deck_name)
        for d in self.deck_results:
            if d.name == deck_name or d.normalized_name == target:
                decisive = self.games_completed - self.draws
                if decisive <= 0:
                    return 0.0
                return d.wins / decisive
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["draws"] = self.draws
        d["avg_game_sec"] = self.avg_game_sec
        d["confirm_action_per_game"] = self.confirm_action_per_game
        return d

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def _parse_match_payload(payload: str) -> list[DeckResult]:
    """Split a Match Result payload like
        "Ai(1)-A: 0 Ai(2)-B: 1 Ai(3)-C: 0 Ai(4)-D: 1"
    into DeckResult records. Tolerates colons inside deck names by anchoring
    on the `Ai(N)-` boundary first and only stripping the trailing `: <wins>`."""
    parts = _AI_SPLIT.split(payload)
    # split returns [pre, seat1, chunk1, seat2, chunk2, ...]; drop pre.
    if not parts:
        return []
    iter_parts = iter(parts[1:])
    out: list[DeckResult] = []
    for seat in iter_parts:
        chunk = next(iter_parts, "")
        m = _TRAILING_WINS.match(chunk)
        if not m:
            continue
        name, wins = m.group(1), m.group(2)
        try:
            out.append(DeckResult(seat=int(seat), name=name.strip(), wins=int(wins)))
        except ValueError:
            continue
    return out


def parse(stdout: str) -> ParsedSim:
    result = ParsedSim()
    if not stdout:
        return result

    # Track the most recent active player from `Phase: Ai(N)-Name ...` lines.
    # When a confirmAction line fires, we attribute it to whoever was active.
    active_normalized: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()

        if not result.raw_match_line:
            m = _MATCH_LINE.match(line)
            if m:
                result.raw_match_line = m.group(1).strip()
                result.deck_results = _parse_match_payload(result.raw_match_line)
                continue

        gm = _GAME_END.search(line)
        if gm:
            result.games_completed += 1
            try:
                result.total_game_ms += int(gm.group(1))
            except ValueError:
                pass
            continue

        pm = _PHASE.match(line)
        if pm:
            active_normalized = _normalize(pm.group(2).strip())
            continue

        um = _UNSUPPORTED.search(line)
        if um:
            result.unsupported_cards.append(um.group(1).strip())
            continue

        cm = _CONFIRM_ACTION.search(line)
        if cm:
            result.confirm_action_cards.append(cm.group(1).strip())
            if active_normalized:
                result.confirm_action_by_deck[active_normalized] = (
                    result.confirm_action_by_deck.get(active_normalized, 0) + 1
                )
            continue

    # Cross-check: if Match Result reported wins but no Game Result lines
    # showed up (some Forge builds quiet them), back-fill from sum of wins.
    if not result.games_completed and result.deck_results:
        total_wins = sum(d.wins for d in result.deck_results)
        if total_wins > 0:
            result.games_completed = total_wins

    return result
