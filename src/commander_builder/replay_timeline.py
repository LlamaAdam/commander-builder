"""FP-016 replay-lite — pure Forge-log → turn-by-turn timeline parser.

Turns one game's Forge stdout chunk into a JSON-friendly timeline the web
Replays viewer renders: a list of turns (turn number + active player), each
carrying the notable events observed during that turn (life-total changes,
eliminations, best-effort spell casts / attacks, AI-struggle markers), plus
the game result.

Scope and honesty notes:

- This parses Forge's *sim stdout* — the same stream ``log_parser`` and
  ``game_analyzer`` already consume — NOT forge_py full game state. It is
  deliberately coarser than the FP-007 slice-5 state-level replay plan
  (which stays parked with FP-001): you get what Forge chose to log, no
  more. In practice that is turns, life totals, eliminations with reasons,
  and the winner — the practical 80%.
- The regex vocabulary is REUSED from ``log_parser`` / ``game_analyzer``
  (imported, not copied) so the replay timeline can never drift from the
  parsers that decide match outcomes. Cast/attack lines are a best-effort
  extension: current captured Forge logs don't reliably include them, so
  the patterns are permissive and simply produce no events when absent.
- Truncated / aborted logs (timeout kills, intra-pod aborts,
  loop_unattributed rows) parse into a PARTIAL timeline with an explicit
  ``truncated: True`` marker instead of raising or fabricating a result.

Pure module: no filesystem, no env, no Forge. ``replay_store`` handles
persistence; ``web/routes_replays.py`` serves the parsed output.
"""

from __future__ import annotations

import re
from typing import Optional

# Authoritative line vocabulary — shared with the outcome parsers so the
# replay view and the scoring pipeline can never disagree on what a line
# means. See game_analyzer's module docstring for the captured evidence
# behind each pattern.
from .game_analyzer import (
    _DRAW,
    _GAME_OUTCOME_LOST,
    _GAME_OUTCOME_TURN,
    _GAME_RESULT_NO_WIN,
    _GAME_RESULT_WIN,
    _LIFE,
    _TURN,
)
from .log_parser import _CONFIRM_ACTION, _UNSUPPORTED

# Best-effort event lines. Forge's headless sim logs are terse — the
# captured corpus contains Turn/Phase/Life/Outcome/Result lines only — but
# some builds emit per-spell and per-combat lines. These patterns pick
# those up when present and match nothing otherwise; fixtures only assert
# on them via explicit synthetic lines.
_CAST = re.compile(
    r"^(?:Stack:\s*)?Ai\((\d+)\)-(.+?)\s+(?:casts?|plays?)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_ATTACK = re.compile(
    r"^(?:Combat:\s*)?Ai\((\d+)\)-(.+?)\s+(?:attacks|declared? attackers?[:\s])\s*(.*)$",
    re.IGNORECASE,
)


def split_games(stdout: str) -> list[dict]:
    """Split a multi-game Forge stdout into per-game chunks.

    Forge logs all of a sim's games into one stream with no reset marker;
    the only reliable game boundary is the closing ``Game Result:`` line
    (same anchor ``game_analyzer.analyze`` uses). Returns a list of
    ``{"text": str, "complete": bool}`` dicts in game order. A trailing
    chunk that contains at least one Turn line but no closing Game Result
    (timeout / abort / hung-loop kill) is returned with
    ``complete=False`` so callers can persist an honest partial log
    instead of dropping it. Boot noise before game 1 stays attached to
    game 1's chunk — the timeline parser ignores unknown lines.
    """
    if not stdout:
        return []
    chunks: list[dict] = []
    current: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        current.append(line)
        if _GAME_RESULT_NO_WIN.match(line):  # also matches the "has won!" form
            chunks.append({"text": "\n".join(current), "complete": True})
            current = []
    # Trailing lines without a closing Game Result: keep them ONLY if a
    # game had verifiably started (>= 1 Turn line). Otherwise it's just
    # boot/exit noise, not a partial game.
    if current and any(_TURN.match(line) for line in current):
        chunks.append({"text": "\n".join(current), "complete": False})
    return chunks


def parse_timeline(text: str) -> dict:
    """Parse ONE game's log chunk into a turn-by-turn timeline dict.

    Shape (all JSON-serializable; seat keys in ``life_totals`` are strings)::

        {
          "players": [{"seat", "name", "starting_life", "ending_life",
                       "eliminated", "loss_reason"}, ...],
          "turns": [{"turn", "seat", "active", "events": [...],
                     "life_totals": {"1": 40, ...}}, ...],
          "pregame_events": [...],       # events seen before Turn 1
          "result": {"winner_seat", "winner_name", "end_turn",
                     "duration_ms", "is_draw", "eliminations": [...]},
          "truncated": bool,
        }

    Event dicts carry a ``type`` discriminator: ``life``, ``elimination``,
    ``cast``, ``attack``, ``confirm_action``, ``unsupported_card``.

    Winner attribution reuses the ONLY trustworthy source (the trailing
    ``Game Result: ... has won!`` clause — see game_analyzer on the
    4-player "everyone won" Game Outcome bug). A chunk with no Game Result
    line parses as ``truncated=True`` with ``winner_* = None`` — an honest
    partial beats a fabricated result. Never raises on malformed input;
    unknown lines are ignored.
    """
    players: dict[int, dict] = {}
    turns: list[dict] = []
    pregame_events: list[dict] = []
    current_turn: Optional[dict] = None
    life_now: dict[int, int] = {}
    end_turn: Optional[int] = None
    winner_seat: Optional[int] = None
    winner_name: Optional[str] = None
    duration_ms: Optional[int] = None
    is_draw = False
    saw_game_result = False

    def _player(seat: int, name: str) -> dict:
        p = players.get(seat)
        if p is None:
            p = {
                "seat": seat,
                "name": name,
                "starting_life": None,
                "ending_life": None,
                "eliminated": False,
                "loss_reason": None,
            }
            players[seat] = p
        return p

    def _events() -> list[dict]:
        return current_turn["events"] if current_turn is not None else pregame_events

    def _close_turn() -> None:
        if current_turn is not None:
            current_turn["life_totals"] = {
                str(seat): life for seat, life in sorted(life_now.items())
            }
            turns.append(current_turn)

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()

        m = _TURN.match(line)
        if m:
            _close_turn()
            turn_n = int(m.group(1))
            seat = int(m.group(2))
            name = m.group(3).strip()
            _player(seat, name)
            current_turn = {
                "turn": turn_n,
                "seat": seat,
                "active": name,
                "events": [],
                "life_totals": {},
            }
            if end_turn is None or turn_n > end_turn:
                end_turn = turn_n
            continue

        m = _LIFE.match(line)
        if m:
            seat = int(m.group(1))
            name = m.group(2).strip()
            before = int(m.group(3))
            after = int(m.group(4))
            p = _player(seat, name)
            if p["starting_life"] is None:
                p["starting_life"] = before
            p["ending_life"] = after
            life_now[seat] = after
            _events().append({
                "type": "life",
                "seat": seat,
                "name": name,
                "from": before,
                "to": after,
            })
            if after <= 0 and not p["eliminated"]:
                p["eliminated"] = True
                p["loss_reason"] = "life total reached 0"
                _events().append({
                    "type": "elimination",
                    "seat": seat,
                    "name": name,
                    "reason": "life total reached 0",
                })
            continue

        m = _GAME_OUTCOME_LOST.match(line)
        if m:
            # End-of-game outcome block — the ONLY signal for non-life
            # eliminations (commander damage / poison / mill / spell),
            # which happen at POSITIVE life. Emitted at game end, so it
            # says nothing about WHEN the seat died (game_analyzer has
            # the full rationale); the event lands on the current turn.
            seat = int(m.group(1))
            name = m.group(2).strip()
            reason = m.group(3).strip() or "unknown reason"
            p = _player(seat, name)
            if not p["eliminated"]:
                p["eliminated"] = True
                p["loss_reason"] = reason
                _events().append({
                    "type": "elimination",
                    "seat": seat,
                    "name": name,
                    "reason": reason,
                })
            elif p["loss_reason"] == "life total reached 0":
                # Refine the inferred life-stream reason with Forge's
                # explicit phrasing (e.g. "because life total reached 0").
                p["loss_reason"] = reason
            continue

        m = _GAME_OUTCOME_TURN.match(line)
        if m:
            end_turn = int(m.group(1))  # authoritative; overrides max-Turn
            continue

        if _DRAW.search(line):
            is_draw = True
            continue

        m = _GAME_RESULT_WIN.match(line)
        if m:
            saw_game_result = True
            duration_ms = int(m.group(2))
            winner_seat = int(m.group(3))
            winner_name = m.group(4).strip()
            continue

        m = _GAME_RESULT_NO_WIN.match(line)
        if m:
            saw_game_result = True
            duration_ms = int(m.group(2))
            continue

        m = _CAST.match(line)
        if m:
            _events().append({
                "type": "cast",
                "seat": int(m.group(1)),
                "name": m.group(2).strip(),
                "spell": m.group(3).strip(),
            })
            continue

        m = _ATTACK.match(line)
        if m:
            _events().append({
                "type": "attack",
                "seat": int(m.group(1)),
                "name": m.group(2).strip(),
                "detail": m.group(3).strip(),
            })
            continue

        m = _CONFIRM_ACTION.search(line)
        if m:
            _events().append({
                "type": "confirm_action",
                "card": m.group(1).strip(),
            })
            continue

        m = _UNSUPPORTED.search(line)
        if m:
            _events().append({
                "type": "unsupported_card",
                "card": m.group(1).strip(),
            })
            continue

    _close_turn()

    return {
        "players": [players[s] for s in sorted(players)],
        "turns": turns,
        "pregame_events": pregame_events,
        "result": {
            "winner_seat": winner_seat,
            "winner_name": winner_name,
            "end_turn": end_turn,
            "duration_ms": duration_ms,
            "is_draw": is_draw,
            "eliminations": [
                {
                    "seat": p["seat"],
                    "name": p["name"],
                    "reason": p["loss_reason"],
                }
                for p in (players[s] for s in sorted(players))
                if p["eliminated"]
            ],
        },
        "truncated": not saw_game_result,
    }
