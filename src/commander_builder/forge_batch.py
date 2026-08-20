"""Multi-sim Forge orchestration harnesses — extracted verbatim from
forge_runner.py on 2026-06-12 so forge_runner keeps its "spawn one Forge
sim" charter. Contains the A/B head-to-head harness, the gauntlet harness,
and the concurrent/parallel pool orchestrators built on top of them.

Canonical import path for downstream code remains
``commander_builder.forge_runner`` (which re-exports every name here) —
e.g. scripts/soak_pool.py's
``from commander_builder.forge_runner import run_gauntlet_simulation``.

ForgeRunner / VENDOR_FORGE are imported from forge_runner lazily inside the
functions that need them (never at module level): forge_runner re-exports
this module's names at its tail, so an eager import here would make
``import commander_builder.forge_batch`` blow up on the circular import
whenever forge_batch loads first.
"""

from __future__ import annotations

import os
import queue
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # typing-only: "ForgeRunner" annotations stay resolvable
    from .forge_runner import ForgeRunner  # noqa: F401

# ---------------------------------------------------------------------------
# A/B simulation harness — old-deck vs new-deck head-to-head
# ---------------------------------------------------------------------------

# Sentinel statuses for ABResult.status. Plain strings (rather than an enum)
# so the dict round-trips cleanly through JSON for the iteration row and the
# UI's status pill can switch on them without an import.
_AB_STATUS_PENDING = "pending"
_AB_STATUS_RUNNING = "running"
_AB_STATUS_DONE = "done"
_AB_STATUS_SKIPPED = "skipped"
_AB_STATUS_FAILED = "failed"
# A gauntlet batch cut short by a looping game that could NOT be credited to
# any seat. Forge's SimulateMatch prints the game log (including every
# "Turn:" line) only AFTER a game completes — a hung/looping game leaves no
# Turn line in the partial stdout, so `_last_active_seat` has nothing to read
# and seat attribution is impossible BY CONSTRUCTION, no matter how faithfully
# the partial stdout was captured (keep_partial_output already routes the kill
# through the streaming reader). The games that DID complete are real data:
# consumers (soak_pool, margin_analysis, merge_soak) treat this status as a
# legitimate SHORT row — completed games count, the hung game is excluded —
# not as a failure.
_AB_STATUS_LOOP_UNATTRIBUTED = "loop_unattributed"

# Default per-game timeout for the A/B sim. Commander games can stall on
# board states that confuse the AI; cap each game at this many seconds so
# one bad game can't hang the whole 5-game batch indefinitely.
_AB_TIMEOUT_PER_GAME_SEC = 180

# A timed-out game is almost always a combo loop or AI hang. We credit the
# game to the "active player" — the seat named in the LAST "Turn:" line of
# the captured stdout (whose turn it is when the loop happens). Matches the
# shape used by game_analyzer._TURN.
_AB_TURN_LINE = re.compile(r"^Turn:\s+Turn\s+(\d+)\s+\(Ai\((\d+)\)-(.+?)\)\s*$")


def _last_active_seat(stdout: str) -> Optional[int]:
    """Return the seat (1-based) named in the LAST 'Turn: Turn N (Ai(M)-...)'
    line of ``stdout``, or None if no Turn line is present. Used to attribute
    a timed-out (looping) game to whoever was the active player."""
    seat: Optional[int] = None
    for raw_line in stdout.splitlines():
        m = _AB_TURN_LINE.match(raw_line.rstrip())
        if m:
            seat = int(m.group(2))
    return seat


@dataclass
class ABResult:
    """Aggregate result of a head-to-head A/B Forge sim.

    Persisted into the iteration row's sim_report so the UI can render
    "Old: 3 wins / New: 2 wins (5 games)" alongside the audit history.
    ``status`` is the lifecycle pill:

    - ``pending`` — queued but not yet started
    - ``running`` — in flight on the background worker
    - ``done`` — completed; ``wins_a`` / ``wins_b`` are authoritative
    - ``skipped`` — Forge unreachable, missing fillers, etc.; no wins
    - ``failed`` — Forge ran but errored; ``error`` carries the reason
    """
    deck_a: str = ""
    deck_b: str = ""
    wins_a: int = 0
    wins_b: int = 0
    games: int = 0
    avg_turns_a: float = 0.0
    avg_turns_b: float = 0.0
    # How many games back each avg_turns_* mean: wins with a KNOWN
    # end_turn and resolved seat. Timeout-salvaged wins carry no end_turn,
    # so these can be smaller than wins_a/wins_b — run_ab_parallel must
    # weight chunk means by THESE counts (weighting by wins skewed the
    # recombined average whenever a chunk held salvaged wins).
    turn_samples_a: int = 0
    turn_samples_b: int = 0
    status: str = _AB_STATUS_PENDING
    error: Optional[str] = None
    duration_sec: float = 0.0
    # The per-game deck_filenames lists we sent to Forge — handy for
    # debugging seat-order alternation and for showing the user which
    # filler decks the harness picked.
    seat_orders: list[list[str]] = field(default_factory=list)
    # Draw-policy label (2026-07-19): this harness resolves turn-cap draws
    # to the surviving life leader and credits them as wins (operator
    # verdict-scoring policy point 1). Compare-based reports
    # (ComparisonReport / MatchupReport / meta_test) count 'plain_draw'
    # instead — the label lets downstream analysis tell the two apart.
    # Label only; no behavior change.
    draw_policy: str = "resolve_survivor_leader"

    def to_dict(self) -> dict:
        return asdict(self)


def run_ab_simulation(
    deck_a_path: Path,
    deck_b_path: Path,
    games: int = 5,
    *,
    runner: Optional["ForgeRunner"] = None,
    fillers: Optional[list[str]] = None,
    game_format: str = "commander",
    timeout_per_game: Optional[int] = None,
) -> ABResult:
    """Run a 5-game (configurable) head-to-head between two decks.

    Alternates seat order across games — game 1 puts ``deck_a`` in
    seat 1, game 2 puts ``deck_b`` in seat 1, … — so first-player
    advantage is balanced over an even number of games.

    Commander format expects a 4-player pod, so the caller must supply
    two filler deck filenames (already present in the Forge userdata
    commander/ directory). The harness skips with status='skipped'
    when fewer than 2 fillers are supplied; same for when ForgeRunner
    can't be located on the host.

    The function never raises — every failure mode lands in the
    returned ABResult so the background worker on /api/save_iteration
    can record it on the iteration row without try/except boilerplate.
    """
    # Lazy imports so a missing optional dep in log_parser/game_analyzer
    # doesn't break ``from forge_runner import ...`` at module import
    # time. (Both are local imports, so cost is one-time per call.)
    from .log_parser import parse as _parse_sim
    from .game_analyzer import analyze as _analyze_match
    # Lazy too — forge_runner re-exports this module, so a module-level
    # import here would be circular (see module docstring).
    from .forge_runner import ForgeRunner

    result = ABResult(
        deck_a=deck_a_path.name,
        deck_b=deck_b_path.name,
        games=0,
        status=_AB_STATUS_PENDING,
    )

    # Locate Forge first — if the host doesn't have it we bail before
    # touching the runner. The save-iteration HTTP response shouldn't
    # care whether Forge is reachable; the worker logs the skip and
    # the UI surfaces 'skipped' in the status pill.
    if runner is None:
        try:
            runner = ForgeRunner.locate()
        except (FileNotFoundError, OSError) as exc:
            result.status = _AB_STATUS_SKIPPED
            result.error = f"Forge not available: {exc}"
            return result

    if game_format == "commander":
        if fillers is None or len(fillers) < 2:
            result.status = _AB_STATUS_SKIPPED
            result.error = (
                "commander A/B sim needs at least 2 filler decks "
                "(got "
                + (str(len(fillers)) if fillers is not None else "0")
                + ")"
            )
            return result
        filler_a = fillers[0]
        filler_b = fillers[1]

    a_turns: list[int] = []
    b_turns: list[int] = []
    started = time.monotonic()
    result.status = _AB_STATUS_RUNNING

    for i in range(games):
        # Alternate seat order — even iters: A first; odd iters: B first.
        # The filler pair stays in seats 3+4 in both cases; only the
        # head-to-head pair flips.
        if game_format == "commander":
            if i % 2 == 0:
                order = [deck_a_path.name, deck_b_path.name, filler_a, filler_b]
            else:
                order = [deck_b_path.name, deck_a_path.name, filler_a, filler_b]
        else:
            order = (
                [deck_a_path.name, deck_b_path.name]
                if i % 2 == 0
                else [deck_b_path.name, deck_a_path.name]
            )
        result.seat_orders.append(order)

        try:
            sim = runner.run(
                deck_filenames=order,
                num_games=1,
                game_format=game_format,
                timeout_sec=timeout_per_game or _AB_TIMEOUT_PER_GAME_SEC,
                # Streaming capture: the timeout salvage below reads
                # sim.stdout for the last Turn line, which the blocking
                # path loses when the timed-out process is killed.
                keep_partial_output=True,
            )
        except Exception as exc:  # noqa: BLE001 — never raise from background
            result.status = _AB_STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.duration_sec = (time.monotonic() - started)
            return result

        # Seat attribution is unambiguous: we built `order`, and Forge seats
        # decks in command-line order (Ai(1)=order[0]). Deck A and deck B
        # frequently share the same internal `Name=` field, so we must NEVER
        # attribute by name — seat only.
        seat_a = order.index(deck_a_path.name) + 1
        seat_b = order.index(deck_b_path.name) + 1

        # TIMEOUT SALVAGE (operator verdict-scoring policy point 2). A single
        # game hitting the per-game wall timeout is almost always a combo loop
        # or AI hang, not a Forge crash. Rather than discarding the whole
        # batch, credit the looping game to the ACTIVE player (the seat in the
        # last "Turn:" line) and finish 'done'. Games tallied earlier in the
        # loop are kept. The subprocess is dead, so we can't continue — return.
        if sim.timed_out:
            active_seat = _last_active_seat(sim.stdout)
            if active_seat == seat_a:
                result.wins_a += 1
                note = f"loop at game {i + 1} credited to active seat {active_seat}"
            elif active_seat == seat_b:
                result.wins_b += 1
                note = f"loop at game {i + 1} credited to active seat {active_seat}"
            elif active_seat is not None:
                note = (
                    f"loop at game {i + 1} credited to filler seat {active_seat} "
                    f"(neither A nor B)"
                )
            else:
                note = f"loop at game {i + 1} credited to none (no Turn line found)"
            result.games = i + 1
            result.status = _AB_STATUS_DONE
            result.error = note
            # Finalize avg_turns from the games completed BEFORE the timeout —
            # otherwise a batch that ran several decisive games then looped on
            # the last one reports avg_turns_a/b = 0.0 (silent data loss).
            # turn_samples_* records how many games back each mean — the
            # salvaged win just credited above has NO end_turn, which is
            # exactly why parallel recombination can't weight by wins.
            if a_turns:
                result.avg_turns_a = round(sum(a_turns) / len(a_turns), 2)
            if b_turns:
                result.avg_turns_b = round(sum(b_turns) / len(b_turns), 2)
            result.turn_samples_a = len(a_turns)
            result.turn_samples_b = len(b_turns)
            result.duration_sec = round((time.monotonic() - started), 2)
            return result

        # Treat any non-zero exit OR captured (non-timeout) error as a genuine
        # failure for the batch — a real Forge crash / NPE is not a loop, so
        # don't salvage it; the dashboard banner is more useful with "failed
        # at game 2/5" than a noisy 1-of-5 partial.
        if sim.error or (sim.returncode is not None and sim.returncode != 0):
            result.status = _AB_STATUS_FAILED
            result.error = sim.error or f"Forge exited with code {sim.returncode}"
            result.duration_sec = (time.monotonic() - started)
            return result

        parsed = _parse_sim(sim.stdout)
        match = _analyze_match(sim.stdout)

        # Attribute wins by SEAT (see seat_a/seat_b above). log_parser's
        # deck_results carry the decisive per-seat wins.
        for d in parsed.deck_results:
            if d.seat == seat_a:
                result.wins_a += d.wins
            elif d.seat == seat_b:
                result.wins_b += d.wins

        # DRAW -> life/board leader (operator verdict-scoring policy point 1).
        # log_parser credits no seat for a turn-cap draw. game_analyzer now
        # resolves such draws to the unique highest-ending_life seat; credit
        # that seat as a win too so a draw won by deck_a's seat counts as a
        # deck_a win. Only games that are is_draw AND have a resolved leader
        # are added here (decisive games are already counted above).
        for g in match.games:
            if not g.is_draw or g.resolved_winner_seat is None:
                continue
            if g.resolved_winner_seat == seat_a:
                result.wins_a += 1
            elif g.resolved_winner_seat == seat_b:
                result.wins_b += 1

        # Per-game turn stats — also seat-based for the same reason. Tally
        # only the games each deck actually won so avg_turns_a reflects "how
        # fast does A close out games it wins", not the average of all games.
        for g in match.games:
            if g.end_turn is None or g.resolved_winner_seat is None:
                continue
            if g.resolved_winner_seat == seat_a:
                a_turns.append(g.end_turn)
            elif g.resolved_winner_seat == seat_b:
                b_turns.append(g.end_turn)

        result.games = i + 1

    if a_turns:
        result.avg_turns_a = round(sum(a_turns) / len(a_turns), 2)
    if b_turns:
        result.avg_turns_b = round(sum(b_turns) / len(b_turns), 2)
    result.turn_samples_a = len(a_turns)
    result.turn_samples_b = len(b_turns)
    result.status = _AB_STATUS_DONE
    result.duration_sec = round((time.monotonic() - started), 2)
    return result


# ---------------------------------------------------------------------------
# Gauntlet simulation harness — ONE test deck vs a FIXED 3-deck gauntlet.
#
# run_ab_simulation seats the two decks under comparison in the SAME pod, so
# they race/target each other and the other two seats are random fillers — the
# "field" is neither controlled nor isolated from the comparison. This harness
# instead seats a single test deck against three FIXED gauntlet decks. To
# compare v1 vs v2 you run each against the IDENTICAL gauntlet and diff their
# win rates: the only thing that changes between the two runs is the deck under
# test, so the delta attributes cleanly to the deck edit (no cannibalization).
# Baseline win rate for a fair 4-player pod is 25%.
# ---------------------------------------------------------------------------


@dataclass
class GauntletResult:
    """One test deck played N games against a fixed 3-deck gauntlet.

    - ``wins``   — games the TEST seat won (decisive + timeout-salvage credited
      to its seat + turn-cap draws resolved to its seat as life leader).
    - ``losses`` — games a GAUNTLET seat won by the same three rules.
    - ``draws``  — games with no resolved winner (true turn-cap draw).

    wins + losses + draws == games for a ``done`` result, and also for a
    ``loop_unattributed`` one: when a looping game times out with no seat
    attributable from the partial stdout (Forge only prints the game log
    after a game completes), the hung game is EXCLUDED from every tally and
    the batch ends early with the completed games kept — an honest short row.
    """
    test_deck: str = ""
    gauntlet: list[str] = field(default_factory=list)
    games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    avg_turns_win: float = 0.0
    status: str = _AB_STATUS_PENDING
    error: Optional[str] = None
    duration_sec: float = 0.0
    seat_orders: list[list[str]] = field(default_factory=list)
    # Same draw-resolution policy label as ABResult — this harness also
    # resolves turn-cap draws to the surviving life leader ('draws' here
    # only counts games with NO resolvable leader). Label only.
    draw_policy: str = "resolve_survivor_leader"


def run_gauntlet_simulation(
    test_deck_path: Path,
    gauntlet_filenames: "list[str]",
    games: int = 40,
    *,
    game_format: str = "commander",
    runner: "Optional[ForgeRunner]" = None,
    timeout_per_game: "Optional[int]" = None,
) -> GauntletResult:
    """Run ``games`` 4-player pods of ``test_deck`` vs a fixed gauntlet.

    ``gauntlet_filenames`` are three deck *filenames* already present in the
    Forge userdata commander/ dir. The test deck is rotated through all four
    seats across games (seat = i % 4 + 1) to cancel turn-order advantage; the
    gauntlet decks fill the remaining seats in fixed order.

    Per-game resolution mirrors run_ab_simulation exactly — timeout salvage to
    the active seat, genuine crash -> failed, decisive win by seat, turn-cap
    draw resolved to the highest-ending-life seat — but tallies from the single
    test seat's point of view. Never raises; failures land in the result.
    """
    from .log_parser import parse as _parse_sim
    from .game_analyzer import analyze as _analyze_match
    # Lazy too — forge_runner re-exports this module, so a module-level
    # import here would be circular (see module docstring).
    from .forge_runner import ForgeRunner

    result = GauntletResult(
        test_deck=test_deck_path.name,
        gauntlet=list(gauntlet_filenames),
        status=_AB_STATUS_PENDING,
    )

    if game_format == "commander" and len(gauntlet_filenames) != 3:
        result.status = _AB_STATUS_SKIPPED
        result.error = (
            f"commander gauntlet sim needs exactly 3 gauntlet decks "
            f"(got {len(gauntlet_filenames)})"
        )
        return result

    if runner is None:
        try:
            runner = ForgeRunner.locate()
        except (FileNotFoundError, OSError) as exc:
            result.status = _AB_STATUS_SKIPPED
            result.error = f"Forge not available: {exc}"
            return result

    win_turns: list[int] = []
    started = time.monotonic()
    result.status = _AB_STATUS_RUNNING

    for i in range(games):
        # Rotate the test deck through all four seats over every 4 games.
        seat_idx = i % 4
        order = list(gauntlet_filenames)
        order.insert(seat_idx, test_deck_path.name)
        result.seat_orders.append(order)
        test_seat = seat_idx + 1

        try:
            sim = runner.run(
                deck_filenames=order,
                num_games=1,
                game_format=game_format,
                timeout_sec=timeout_per_game or _AB_TIMEOUT_PER_GAME_SEC,
                # Streaming capture — same reason as run_ab_simulation: the
                # salvage path needs the pre-kill stdout for seat attribution.
                keep_partial_output=True,
            )
        except Exception as exc:  # noqa: BLE001 — never raise from a worker
            result.status = _AB_STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.duration_sec = (time.monotonic() - started)
            return result

        # TIMEOUT SALVAGE (same policy as run_ab_simulation): credit the
        # looping game to the active seat — win if that's the test seat, loss
        # if it's a gauntlet seat. When NO Turn line is present in the partial
        # stdout there is no seat to credit: Forge's SimulateMatch prints the
        # game log only after a game completes, so a hung game leaves nothing
        # to attribute (see _AB_STATUS_LOOP_UNATTRIBUTED). The old behavior
        # counted that unattributable game as a DRAW and reported "credited to
        # active seat None" — a misleading error on a phantom game. Now the
        # hung game is excluded entirely and the row ends as an honest short
        # 'loop_unattributed' row with the completed games kept. Subprocess is
        # dead either way, so we stop the batch here with what we have.
        if sim.timed_out:
            active_seat = _last_active_seat(sim.stdout)
            if active_seat is None:
                result.games = i
                result.status = _AB_STATUS_LOOP_UNATTRIBUTED
                result.error = (
                    f"loop at game {i + 1}: no seat attributable from partial "
                    f"stdout (Forge prints the game log only after a game "
                    f"completes); kept {i} completed games"
                )
            else:
                if active_seat == test_seat:
                    result.wins += 1
                else:
                    result.losses += 1
                result.games = i + 1
                result.status = _AB_STATUS_DONE
                result.error = (
                    f"loop at game {i + 1} credited to active seat {active_seat}"
                )
            # Finalize avg_turns_win from games completed before the timeout
            # (otherwise it's silently reported as 0.0 on the salvage path).
            if win_turns:
                result.avg_turns_win = round(sum(win_turns) / len(win_turns), 2)
            result.duration_sec = round((time.monotonic() - started), 2)
            return result

        if sim.error or (sim.returncode is not None and sim.returncode != 0):
            result.status = _AB_STATUS_FAILED
            result.error = sim.error or f"Forge exited with code {sim.returncode}"
            result.duration_sec = (time.monotonic() - started)
            return result

        parsed = _parse_sim(sim.stdout)
        match = _analyze_match(sim.stdout)

        # Decisive winner: log_parser credits the winning seat with d.wins (==1
        # in a 1-game sim). Attribute by SEAT, never by name.
        resolved_seat = None
        end_turn = None
        for d in parsed.deck_results:
            if d.wins:
                resolved_seat = d.seat
                break
        if resolved_seat is None:
            # No decisive win -> resolve a turn-cap draw to the life leader.
            for g in match.games:
                if g.is_draw and g.resolved_winner_seat is not None:
                    resolved_seat = g.resolved_winner_seat
                    end_turn = g.end_turn
                    break
        else:
            for g in match.games:
                if g.resolved_winner_seat == resolved_seat:
                    end_turn = g.end_turn
                    break

        if resolved_seat == test_seat:
            result.wins += 1
            if end_turn is not None:
                win_turns.append(end_turn)
        elif resolved_seat is not None:
            result.losses += 1
        else:
            result.draws += 1

        result.games = i + 1

    if win_turns:
        result.avg_turns_win = round(sum(win_turns) / len(win_turns), 2)
    result.status = _AB_STATUS_DONE
    result.duration_sec = round((time.monotonic() - started), 2)
    return result


# ---------------------------------------------------------------------------
# CROSS-INVOCATION PROFILE LOCKING (2026-08-16)
#
# The pool orchestrators below guarantee that two chunks never share a Forge
# profile — they'd collide on the deck dir, cache and forge.log. But that
# guarantee only ever held WITHIN one invocation: the free-queue is a per-call
# local and profile discovery re-enumerates vendor/forge* every call. The web
# UI launches background sim jobs (/api/propose_swap_async), so a second web
# job — or a CLI run started while one is in flight — happily double-books the
# profile a live JVM is already writing.
#
# Fix: a per-profile ADVISORY lockfile, ``vendor/<profile>/.commander-builder
# .lock``, held for the whole duration of a run and released in a ``finally``
# on every exit path. Design notes, in the order they'll be questioned:
#
# * ATOMICITY PRIMITIVE — ``os.open(O_CREAT | O_EXCL)``. It is atomic on NTFS
#   and on every POSIX filesystem we care about, needs no third-party package,
#   and (unlike ``fcntl.flock``) EXISTS ON WINDOWS, which is this app's primary
#   desktop target. We deliberately do NOT use fcntl/msvcrt locking: those are
#   per-platform, and msvcrt's byte-range locks die with the owning handle,
#   which would make the lock invisible to a second process that merely wants
#   to *look* at whether a profile is busy.
#
# * WHY ADVISORY — nothing stops a rogue JVM from writing the profile anyway.
#   The lock coordinates OUR orchestrators with each other; that is the whole
#   double-booking failure mode we observed.
#
# * STALE POLICY — mtime, not pid liveness. A JVM that is SIGKILLed (or a box
#   that loses power) leaves the lockfile behind, and a profile bricked forever
#   is worse than an occasional double-book. Any lock whose mtime is older than
#   ``_PROFILE_LOCK_STALE_SEC`` is RENAMED to a unique name (the arbitration
#   step — ``_reclaim_stale_lock``) and the profile is then re-acquired through
#   the SAME O_EXCL create, so two runs reclaiming one abandoned lock still
#   can't both win. Corrected 2026-08-20 (R2-P15): the original reclaim
#   unlinked by PATH, which made that last sentence false — the loser's unlink
#   could delete the winner's fresh lock. Releasing verifies the payload for
#   the mirror-image failure (a reclaimed holder deleting its successor's
#   lock). We chose mtime over pid-liveness checks on purpose: psutil is not a
#   hard dependency here, os.kill(pid, 0) does not exist on Windows, and raw
#   pid checks are wrong after pid reuse anyway. The pid/host/timestamp payload
#   we write is DIAGNOSTICS for a human reading the file — never the policy.
#
#   Sizing: 6h. A per-chunk sim is bounded by the 180s/game cap, so the
#   worst realistic hold is a fully serial 100-game commander test (~5h). A
#   multi-day soak run is the one workload that could outlive the window and
#   see its own lock reclaimed under it — soak_pool drives its own profile pool
#   directly, and if that ever changes, bump this constant rather than adding a
#   heartbeat: a stale window shorter than the longest legitimate hold is the
#   only way this policy misfires.
#
# * NOT RE-ENTRANT, BY DESIGN — locking lives at the profile-CHECKOUT layer
#   (run_ab_batch / run_ab_parallel / any caller building a pool), never inside
#   run_ab_simulation or run_gauntlet_simulation. If a per-sim call also
#   locked, a parallel chunk would deadlock against the pool lock its own
#   parent already holds.
#
# * UNLOCKABLE PROFILE == UNFENCED, NOT UNUSABLE — a profile dir that does not
#   exist (every offline test's fake pool) or is read-only gets a no-op lock:
#   there is no live Forge there to collide with, and an unwritable vendor dir
#   must never take the whole sim offline. The intra-invocation free-queue
#   still fences those.
# ---------------------------------------------------------------------------

#: Lockfile basename, created inside the profile dir itself so the lock travels
#: with the profile (moving/deleting the profile disposes of its lock too).
_PROFILE_LOCK_NAME = ".commander-builder.lock"

#: Locks older than this are considered abandoned and reclaimed. Generous on
#: purpose — see the STALE POLICY note above.
_PROFILE_LOCK_STALE_SEC = 6 * 60 * 60


class ProfileLockError(RuntimeError):
    """Every Forge profile on this host is locked by another live sim.

    Raised by :func:`acquire_profile_pool` instead of queueing forever, so a
    second web job / CLI run fails FAST with an actionable message rather than
    hanging on a profile that will not free up for an hour.
    """


@dataclass
class ProfileLock:
    """A held advisory lock on one Forge profile.

    ``path`` is None for a profile we could not create a lockfile in (missing
    or read-only dir) — the lock is then a no-op that still round-trips through
    every acquire/release path so callers need no special-casing.
    """
    profile: Path
    path: Optional[Path] = None
    #: True when this lock took over an abandoned (stale) lockfile.
    reclaimed_stale: bool = False
    released: bool = False
    #: The exact bytes WE wrote into the lockfile. ``release`` compares the
    #: file against this before unlinking, so a run that outlived the stale
    #: window (and was legitimately reclaimed) can no longer delete its
    #: SUCCESSOR's lock on the way out — see the R2-P15 note on ``release``.
    payload: Optional[str] = None

    def _file_is_ours(self) -> bool:
        """Does the lockfile still carry OUR payload?

        An unreadable or EMPTY file counts as ours: ``_create_lock_file``
        creates the file first and writes the payload second, and that write
        is allowed to fail (it is diagnostics, per the module notes).
        Treating empty as foreign would leak the lock for a full stale
        window on a filesystem that refused the write. Any OTHER content
        means a different run created the file after ours was reclaimed.
        """
        if self.path is None or self.payload is None:
            return True
        try:
            body = self.path.read_text(encoding="utf-8")
        except OSError:
            return True
        return body == "" or body == self.payload

    def release(self) -> None:
        """Drop the lock. Idempotent, and never raises — a release that fails
        must not mask the exception that sent us into the ``finally``.

        Round-2 review 2026-08-20 (R2-P15): this used to unlink BY PATH with
        no ownership check. A run that outlives ``_PROFILE_LOCK_STALE_SEC``
        (a wedged JVM, or a fully serial sim past the 6h window) has its lock
        reclaimed by a second run, keeps going, and its ``finally`` then
        deleted the RECLAIMER's live lock — letting a third run in and
        double-booking the profile this mechanism exists to fence. The
        payload check closes that: we only ever delete the file we wrote.
        """
        if self.released:
            return
        self.released = True
        if self.path is None:
            return
        if not self._file_is_ours():
            # Our lock was stale-reclaimed while we ran. The file at this path
            # belongs to the run that reclaimed it; deleting it would unfence
            # a live sim.
            return
        try:
            os.unlink(self.path)
        except OSError:
            # Already gone (stale-reclaimed by someone else), or the dir went
            # read-only mid-run. Nothing useful to do; the mtime policy will
            # clear any leftover.
            pass

    def __enter__(self) -> "ProfileLock":
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def _profile_lock_path(profile: "Path | str") -> Path:
    """Path of ``profile``'s advisory lockfile (may not exist)."""
    return Path(profile) / _PROFILE_LOCK_NAME


def _lock_payload() -> str:
    """Diagnostics written INSIDE the lockfile — for a human, never for STALE
    policy (that stays mtime-based; see the module notes).

    The ``lock_id`` line is the one non-diagnostic part: since 2026-08-20
    (R2-P15) ``ProfileLock.release`` compares the file against the payload it
    wrote before unlinking, and pid+host+second is NOT unique — the same
    process reclaiming a lock inside the same second would produce identical
    bytes and could then delete its successor's file, which is the exact bug
    the check exists to stop. A uuid4 makes each acquisition distinguishable.
    """
    return (
        f"pid={os.getpid()}\n"
        f"host={socket.gethostname()}\n"
        f"lock_id={uuid.uuid4().hex}\n"
        f"acquired_at={time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"acquired_epoch={time.time():.0f}\n"
    )


def _create_lock_file(
    lock_path: Path, payload: "Optional[str]" = None,
) -> "Optional[bool]":
    """Atomically create ``lock_path``, writing ``payload`` into it.

    Returns True when WE created it, False when it already existed (busy), and
    None when lockfiles are unusable at this path at all (dir missing,
    read-only, exotic FS) — the caller treats that as "unfenced but usable".

    ``payload`` defaults to a freshly generated one; callers that need to
    REMEMBER what they wrote (so ``ProfileLock.release`` can verify ownership
    — R2-P15) generate it themselves and pass it in.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return None
    try:
        os.write(fd, (payload or _lock_payload()).encode("utf-8"))
    except OSError:  # noqa: BLE001 — payload is diagnostics; the lock is the file
        pass
    finally:
        os.close(fd)
    return True


def _reclaim_stale_lock(
    lock_path: Path, *, stale_after: float = _PROFILE_LOCK_STALE_SEC,
) -> bool:
    """Take the abandoned lockfile at ``lock_path`` out of the way, atomically.

    Returns True when THIS caller removed a genuinely stale lock (the path is
    now the O_EXCL create's problem), False when it did not — because another
    reclaimer got there first, or because the file turned out to be live.

    Round-2 review 2026-08-20 (R2-P15). The old reclaim was
    ``stat -> os.unlink(lock_path) -> create``, which is a TOCTOU: unlink
    targets a PATH, not the specific stale file, so the interleaving
    ``A:stat, B:stat, A:unlink, A:create, B:unlink, B:create`` has B deleting
    A's FRESH lock and both runs believing they own the profile. The
    docstring's "exactly one of them wins" was false for that ordering.

    The arbitration primitive here is ``os.rename`` to a UNIQUE name: only
    one caller can rename a given file away, and the loser's rename fails
    with ENOENT instead of silently destroying the winner's new lock. Two
    checks make it safe end to end:

    1. After the rename we re-stat what we actually got. If it is FRESH, we
       renamed a live lock created between our stat and our rename — so we
       put it back (see ``_restore_lock``) and report failure.
    2. The caller still treats the following O_EXCL create as the real
       arbiter, exactly as before: winning the rename is permission to try,
       not the lock itself.

    Never raises — a failed reclaim degrades to "busy", which is the safe
    direction for a fence.
    """
    tmp = lock_path.with_name(
        f"{lock_path.name}.reclaim.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        os.rename(str(lock_path), str(tmp))
    except OSError:
        # Gone, or another reclaimer renamed it first. Either way this caller
        # is not the one that cleared the path.
        return False

    age = _lock_age_sec(tmp)
    if age is None or age > stale_after:
        # Genuinely abandoned (or it vanished under us) — drop the corpse.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return True

    # We grabbed a LIVE lock. Put it back and stand down.
    _restore_lock(tmp, lock_path)
    return False


def _restore_lock(tmp: Path, lock_path: Path) -> None:
    """Put a mistakenly-renamed live lockfile back, without clobbering.

    ``os.link`` (not ``os.replace``) on purpose: link FAILS when the
    destination exists, so if a third run has already created its own lock at
    ``lock_path`` in the microseconds we held the file, we leave that lock
    alone and discard our copy. The displaced owner is protected by the
    payload check in ``ProfileLock.release`` either way — it will decline to
    delete a file it did not write.
    """
    try:
        os.link(str(tmp), str(lock_path))
    except OSError:
        # Destination taken (a newer lock owns the path) or hardlinks are
        # unsupported here. Best effort: restore only if the path is free.
        if not lock_path.exists():
            try:
                os.rename(str(tmp), str(lock_path))
                return
            except OSError:
                pass
    try:
        os.unlink(tmp)
    except OSError:
        pass


def _lock_age_sec(lock_path: Path) -> "Optional[float]":
    """Seconds since ``lock_path`` was last written, or None if it's gone."""
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return None


def is_profile_locked(
    profile: "Path | str",
    *,
    stale_after: float = _PROFILE_LOCK_STALE_SEC,
) -> bool:
    """True when ``profile`` carries a LIVE (non-stale) lock.

    A cheap advisory peek for discovery/UI. It is NOT the fence — only
    :func:`_try_acquire_profile` is, since any check-then-act is racy.
    """
    age = _lock_age_sec(_profile_lock_path(profile))
    return age is not None and age <= stale_after


def _try_acquire_profile(
    profile: "Path | str",
    *,
    stale_after: float = _PROFILE_LOCK_STALE_SEC,
) -> "Optional[ProfileLock]":
    """Atomically take ``profile``'s lock; None when another sim holds it.

    Stale locks (mtime older than ``stale_after``) are RENAMED out of the way
    (``_reclaim_stale_lock`` — atomic arbitration between simultaneous
    reclaimers) and the profile is then re-acquired through the same O_EXCL
    create, which stays the real arbiter. Losing either step reports busy,
    which is the safe direction for a fence.
    """
    profile = Path(profile)
    if not profile.is_dir():
        # No profile dir -> no Forge instance to collide with (this is every
        # offline test's fake pool). Hand back a no-op lock.
        return ProfileLock(profile=profile, path=None)

    lock_path = _profile_lock_path(profile)
    payload = _lock_payload()
    state = _create_lock_file(lock_path, payload)
    if state is None:
        return ProfileLock(profile=profile, path=None)
    if state:
        return ProfileLock(profile=profile, path=lock_path, payload=payload)

    # Busy. Stale?
    age = _lock_age_sec(lock_path)
    if age is not None and age <= stale_after:
        return None
    # age is None -> the holder released it between our create and our stat;
    # fall through and let the O_EXCL retry decide (it is the real arbiter).
    if age is not None and not _reclaim_stale_lock(
        lock_path, stale_after=stale_after,
    ):
        # Another reclaimer won the rename, or the lock turned out to be
        # live. Report busy rather than racing the winner: the previous code
        # retried the create here, which is how two runs could both end up
        # "holding" one profile (R2-P15).
        return None
    payload = _lock_payload()
    state = _create_lock_file(lock_path, payload)
    if state is None:
        return ProfileLock(profile=profile, path=None)
    if state:
        return ProfileLock(
            profile=profile, path=lock_path, payload=payload,
            reclaimed_stale=age is not None,
        )
    return None


def _all_locked_message(
    profiles: "list[Path]",
    *,
    stale_after: float = _PROFILE_LOCK_STALE_SEC,
) -> str:
    """Actionable "everything is busy" text naming the OLDEST lock to inspect."""
    oldest_path: Optional[Path] = None
    oldest_age = -1.0
    for p in profiles:
        lp = _profile_lock_path(p)
        age = _lock_age_sec(lp)
        if age is not None and age > oldest_age:
            oldest_age, oldest_path = age, lp
    if oldest_path is None:
        where = "no lock file readable"
    else:
        where = f"oldest lock at {oldest_path} ({oldest_age / 60:.0f} min old)"
    return (
        f"{len(profiles)} Forge profile(s), all locked; another sim running? "
        f"{where} — delete if no sim is active. Locks older than "
        f"{stale_after / 3600:.0f}h are reclaimed automatically."
    )


def acquire_profile_pool(
    profiles: "list[Path]",
    count: Optional[int] = None,
    *,
    stale_after: float = _PROFILE_LOCK_STALE_SEC,
) -> "list[ProfileLock]":
    """Lock up to ``count`` FREE profiles from ``profiles``, in order.

    Profiles already locked by another process are skipped (busy — take the
    next free one), so a host with 12 profiles and one running sim still fans
    out across the other 11. Raises :class:`ProfileLockError` when NOTHING is
    free rather than blocking: a caller waiting on a profile that a multi-hour
    soak owns is indistinguishable from a hang.

    The returned locks are the caller's to release — always in a ``finally``.
    """
    profs = [Path(p) for p in profiles]
    if not profs:
        raise ProfileLockError(
            "no Forge profiles to lock (expected vendor/forge[, forge2..N])",
        )
    want = len(profs) if count is None else max(1, min(int(count), len(profs)))

    locked: "list[ProfileLock]" = []
    for p in profs:
        if len(locked) >= want:
            break
        lk = _try_acquire_profile(p, stale_after=stale_after)
        if lk is not None:
            locked.append(lk)
    if not locked:
        raise ProfileLockError(_all_locked_message(profs, stale_after=stale_after))
    return locked


def release_profile_pool(locks: "list[ProfileLock]") -> None:
    """Release every lock in ``locks``. Never raises (see ProfileLock.release)."""
    for lk in locks:
        lk.release()


# ---------------------------------------------------------------------------
# Concurrent A/B sims (FP-003) — run N head-to-heads across a pool of
# cwd-isolated Forge profiles, capping concurrency at the number of profiles.
# ---------------------------------------------------------------------------


@dataclass
class ABJob:
    """One head-to-head to run. ``deck_a``/``deck_b`` are deck file paths;
    ``fillers`` are the two filler deck *filenames* (commander pods need 4).
    ``games``/``game_format`` override the batch defaults per-job when set."""
    deck_a: Path
    deck_b: Path
    fillers: Optional[list[str]] = None
    games: Optional[int] = None
    game_format: Optional[str] = None


def run_ab_batch(
    jobs: "list[ABJob]",
    runners: "list[ForgeRunner]",
    *,
    games: int = 5,
    game_format: str = "commander",
    _sim_fn: "Callable[..., ABResult]" = run_ab_simulation,
) -> "list[ABResult]":
    """Run several A/B sims concurrently, one per cwd-isolated profile.

    ``runners`` is a pool of ForgeRunners, each pointing at a DISTINCT
    profile dir (see ForgeRunner.for_profile + the vendor/forge2 setup).
    Concurrency is capped at ``len(runners)`` and a runner is never handed
    to two jobs at once — that's the whole point, since two Forge instances
    in the same profile would collide on the deck dir, cache, and forge.log.

    Results are returned in the SAME ORDER as ``jobs`` (not completion
    order). Like ``run_ab_simulation``, individual jobs never raise — a
    failure lands in that job's ABResult; only a misconfigured pool (no
    runners) or a fully locked pool (ProfileLockError) raises.

    CROSS-PROCESS FENCE: the free-queue below only fences the jobs of THIS
    call, so before running anything we take each runner's profile lock (see
    the locking section above) and hold it for the whole batch. A runner whose
    profile another process already owns is dropped from the pool — the batch
    runs narrower rather than double-booking a live JVM's profile — and the
    locks are released in a ``finally`` on every exit path.

    ``_sim_fn`` is injectable so the pool logic can be unit-tested without
    Forge."""
    if not runners:
        raise ValueError("run_ab_batch needs at least one runner.")
    if not jobs:
        return []

    usable, locks = _lock_runner_pool(runners)
    try:
        free: "queue.Queue[ForgeRunner]" = queue.Queue()
        for r in usable:
            free.put(r)

        results: "list[Optional[ABResult]]" = [None] * len(jobs)

        def _do(idx: int, job: ABJob):
            runner = free.get()  # blocks until a profile is free (never, in practice,
            # since max_workers == len(usable), but keeps the invariant explicit)
            try:
                res = _sim_fn(
                    job.deck_a,
                    job.deck_b,
                    games=job.games if job.games is not None else games,
                    runner=runner,
                    fillers=job.fillers,
                    game_format=job.game_format or game_format,
                )
                results[idx] = res
            finally:
                free.put(runner)

        with ThreadPoolExecutor(max_workers=len(usable)) as ex:
            futures = [ex.submit(_do, i, job) for i, job in enumerate(jobs)]
            for f in futures:
                f.result()  # surface unexpected (non-ABResult) exceptions

        return results  # type: ignore[return-value]
    finally:
        release_profile_pool(locks)


def _lock_runner_pool(
    runners: "list[ForgeRunner]",
) -> "tuple[list[ForgeRunner], list[ProfileLock]]":
    """Take the profile lock behind every runner; drop the busy ones.

    Returns ``(usable_runners, locks)``. A runner with no ``forge_dir`` (every
    test double) is kept unfenced — there is no profile to collide over. Two
    runners pointing at the SAME profile are also deduplicated by this, since
    the second one sees our own lock and is dropped: exactly the "never hand
    one profile to two jobs" invariant, now enforced across processes too.

    Raises :class:`ProfileLockError` when every profile-bound runner is busy.
    """
    usable: "list[ForgeRunner]" = []
    locks: "list[ProfileLock]" = []
    busy: "list[Path]" = []
    try:
        for r in runners:
            forge_dir = getattr(r, "forge_dir", None)
            if forge_dir is None:
                usable.append(r)
                continue
            lk = _try_acquire_profile(forge_dir)
            if lk is None:
                busy.append(Path(forge_dir))
                continue
            locks.append(lk)
            usable.append(r)
        if not usable:
            raise ProfileLockError(_all_locked_message(busy))
    except BaseException:
        release_profile_pool(locks)
        raise
    return usable, locks


# ---------------------------------------------------------------------------
# Parallel single-matchup A/B (the "100-game commander test" speedup).
#
# run_ab_simulation runs its `games` serially in ONE Forge process per game,
# so a 100-game commander test pins a single core for ~an hour. The games are
# independent, so we can split them into chunks, run one chunk per cwd-isolated
# Forge profile concurrently, and sum the per-seat wins back into a single
# ABResult that's identical in shape to a serial run. On a box with P profiles
# and C cores this is a ~min(P, C)x wall-clock win (12 profiles here -> ~5 min).
# ---------------------------------------------------------------------------


def _discover_profiles(max_n: int = 64, *, skip_locked: bool = True) -> "list[Path]":
    """All existing cwd-isolated Forge profiles: vendor/forge, vendor/forge2..N.

    Mirrors the layout soak_pool.py relies on — vendor/forge is profile 1 and
    vendor/forge{i} (i>=2) are the extras. Only directories that actually exist
    are returned, so concurrency can never exceed the profiles on this host.

    ``skip_locked`` (default on) also hides profiles a DIFFERENT process is
    currently simulating in — see the cross-invocation locking section above.
    That keeps a second sim job from ever planning work on a busy profile;
    the acquisition in :func:`acquire_profile_pool` remains the authoritative
    fence, since any check-then-act peek is inherently racy. Pass
    ``skip_locked=False`` to enumerate the raw layout (used to tell "this host
    has no profiles at all" apart from "every profile is busy").
    """
    # Lazy — forge_runner re-exports this module (see module docstring).
    from .forge_runner import VENDOR_FORGE

    out = [VENDOR_FORGE]
    for i in range(2, max_n + 1):
        p = VENDOR_FORGE.parent / f"forge{i}"
        if p.is_dir():
            out.append(p)
    if skip_locked:
        out = [p for p in out if not is_profile_locked(p)]
    return out


def _runner_for(profile: Path) -> "ForgeRunner":
    """ForgeRunner bound to ``profile``'s cwd (shares the located java + jar)."""
    # Lazy — forge_runner re-exports this module (see module docstring).
    from .forge_runner import ForgeRunner, VENDOR_FORGE

    return ForgeRunner.locate() if profile == VENDOR_FORGE else ForgeRunner.for_profile(profile)


def _default_max_workers() -> int:
    """Best default worker count for CPU-bound Forge sims: PHYSICAL cores.

    Benchmarked on a 12-core/24-thread Ryzen 9 3900X: 24 concurrent Forge JVMs
    finished a fixed workload no faster than 12 (293s vs 292s) because each game
    is CPU-bound and SMT/hyperthreads add ~nothing — past one JVM per physical
    core, every game just runs proportionally slower. So we cap at physical
    cores. Uses psutil (a project dependency) when present; falls back to
    logical//2 (the usual SMT ratio), then logical, when it can't be detected.
    Callers can always override with ``max_workers``.
    """
    try:
        import psutil  # project dep (soak_pool); soft-imported so the lib
        phys = psutil.cpu_count(logical=False)  # doesn't hard-require it here
        if phys:
            return phys
    except Exception:  # noqa: BLE001
        pass
    import os as _os
    logical = _os.cpu_count() or 1
    return max(1, logical // 2) if logical > 1 else 1


def _even_chunks(total: int, parts: int) -> "list[int]":
    """Split ``total`` games into at most ``parts`` balanced, EVEN-sized chunks.

    Even sizes matter: run_ab_simulation alternates A-first/B-first by its
    internal game index, so an odd-sized chunk hands deck A one extra first-seat
    game. We split by A/B *pairs* (each pair = one A-first + one B-first game) so
    every chunk stays seat-balanced. For an odd ``total`` the single leftover
    game lands on the first chunk — an unavoidable 1-game seat skew, no worse
    than a serial odd-count run.
    """
    if total < 1:
        return []
    parts = max(1, min(parts, total))
    pairs, leftover = divmod(total, 2)  # leftover is 0 or 1
    base, extra = divmod(pairs, parts)
    sizes = [(base + (1 if k < extra else 0)) * 2 for k in range(parts)]
    if leftover:
        sizes[0] += 1
    return [s for s in sizes if s > 0]


def run_ab_parallel(
    deck_a_path: Path,
    deck_b_path: Path,
    games: int = 100,
    *,
    fillers: Optional[list[str]] = None,
    game_format: str = "commander",
    timeout_per_game: Optional[int] = None,
    max_workers: Optional[int] = None,
    profiles: "Optional[list[Path]]" = None,
    _sim_fn: "Callable[..., ABResult]" = run_ab_simulation,
) -> ABResult:
    """Run a single ``games``-game A/B matchup in parallel across Forge profiles.

    Drop-in faster replacement for ``run_ab_simulation`` when you want one big
    head-to-head (e.g. the 100-game commander test) to finish in wall-clock
    ``games / min(profiles, cores)`` time instead of running every game on one
    core. The games are split into even chunks (see ``_even_chunks``), each chunk
    runs as its own ``run_ab_simulation`` on a distinct cwd-isolated profile, and
    the per-seat wins / turn stats are summed back into ONE ABResult with the
    same fields a serial run would have produced.

    Auto-sizing: workers default to ``min(physical_cores, len(profiles),
    games)`` — physical, not logical, because SMT threads don't speed up these
    CPU-bound JVMs (benchmarked: 24 workers == 12 on a 12c/24t part). Pass
    ``max_workers`` to override. ``profiles`` defaults to every vendor/forge*
    profile on the host that no OTHER process is currently simulating in;
    two chunks never share a profile (they'd collide on the deck dir, cache, and
    forge.log). With a single profile this degenerates to one serial chunk.

    That last guarantee used to hold only WITHIN one invocation — a second web
    sim job or a concurrent CLI run could double-book a profile a live JVM was
    already writing. Each profile we use is now held under an advisory lockfile
    for the whole run (see the locking section above) and released in a
    ``finally``; when every profile is locked the result comes back ``failed``
    with the "all locked; oldest lock at <path> — delete if no sim is active"
    message rather than hanging.

    Like ``run_ab_simulation`` it never raises — per-chunk failures are folded
    into the aggregate ``status``/``error`` and the wins from completed chunks
    are still reported (a crash in chunk 3 doesn't discard 90 good games).
    """
    # Late-bound through forge_runner so tests that stub the runner pool via
    # monkeypatch.setattr("commander_builder.forge_runner._runner_for", ...)
    # keep working after the 2026-06-12 module split.
    from .forge_runner import _runner_for

    result = ABResult(
        deck_a=deck_a_path.name,
        deck_b=deck_b_path.name,
        games=0,
        status=_AB_STATUS_PENDING,
    )
    if games < 1:
        result.status = _AB_STATUS_SKIPPED
        result.error = "games must be >= 1"
        return result

    if profiles is None:
        profiles = _discover_profiles()
        if not profiles:
            # Discovery hides profiles another process is simulating in. Tell
            # "no Forge here" apart from "every profile is busy" — the second
            # one is an operator-actionable condition, not a missing install.
            raw = _discover_profiles(skip_locked=False)
            if raw:
                result.status = _AB_STATUS_FAILED
                result.error = _all_locked_message(raw)
                return result
    if not profiles:
        result.status = _AB_STATUS_SKIPPED
        result.error = "no Forge profiles found (expected vendor/forge[, forge2..N])"
        return result

    cap = min(_default_max_workers(), len(profiles), games)
    if max_workers is not None:
        # An explicit max_workers overrides the physical-core default but is
        # still bounded by available profiles and the game count.
        cap = max(1, min(max_workers, len(profiles), games))

    # CROSS-PROCESS FENCE — take the lock on each profile we intend to use and
    # hold it until every chunk has finished. A profile another sim owns is
    # skipped (we simply fan out narrower); if NOTHING is free we fail fast
    # with an actionable message instead of queueing behind a multi-hour soak.
    # run_ab_parallel never raises, so ProfileLockError lands in the result.
    try:
        locks = acquire_profile_pool(profiles, cap)
    except ProfileLockError as exc:
        result.status = _AB_STATUS_FAILED
        result.error = str(exc)
        return result

    try:
        held = [lk.profile for lk in locks]
        sizes = _even_chunks(games, len(held))
        parts = len(sizes)
        runners = [_runner_for(p) for p in held[:parts]]

        result.status = _AB_STATUS_RUNNING
        started = time.monotonic()

        # One chunk per runner — a dedicated profile each, so no queue/handoff
        # is needed (unlike run_ab_batch, which multiplexes many jobs over few
        # runners). Threads are fine: each chunk blocks in subprocess.run
        # waiting on its JVM, with the GIL released.
        chunk_results: "list[Optional[ABResult]]" = [None] * parts

        def _do(idx: int, size: int, runner: "ForgeRunner"):
            chunk_results[idx] = _sim_fn(
                deck_a_path,
                deck_b_path,
                games=size,
                runner=runner,
                fillers=fillers,
                game_format=game_format,
                timeout_per_game=timeout_per_game,
            )

        with ThreadPoolExecutor(max_workers=parts) as ex:
            futures = [ex.submit(_do, i, sz, runners[i]) for i, sz in enumerate(sizes)]
            for f in futures:
                f.result()  # surface unexpected (non-ABResult) exceptions
    finally:
        # EVERY exit path — clean finish, per-game timeout kill, an unexpected
        # exception out of _runner_for or a chunk future — drops the locks, so
        # a crash can never brick the profiles for the next run.
        release_profile_pool(locks)

    # --- aggregate the chunks back into one ABResult -----------------------
    a_turn_weight = b_turn_weight = 0.0
    statuses: list[str] = []
    errors: list[str] = []
    for ci, res in enumerate(chunk_results):
        if res is None:  # _do always assigns, but stay defensive
            statuses.append(_AB_STATUS_FAILED)
            errors.append(f"chunk {ci}: no result")
            continue
        statuses.append(res.status)
        result.wins_a += res.wins_a
        result.wins_b += res.wins_b
        result.games += res.games
        result.seat_orders.extend(res.seat_orders)
        result.turn_samples_a += res.turn_samples_a
        result.turn_samples_b += res.turn_samples_b
        # Weight each chunk's avg_turns by its turn-SAMPLE count — the games
        # that actually entered that chunk's mean (wins with a known
        # end_turn) — so the combined mean is a true per-sample average.
        # Weighting by wins was wrong: a timeout-salvaged win has NO
        # end_turn, so a chunk's wins can exceed its samples and its mean
        # got over-weighted (or, with avg_turns=0.0 from an all-salvage
        # chunk, dragged the combined mean toward zero).
        a_turn_weight += res.avg_turns_a * res.turn_samples_a
        b_turn_weight += res.avg_turns_b * res.turn_samples_b
        if res.error:
            errors.append(f"chunk {ci} ({res.status}): {res.error}")

    if result.turn_samples_a:
        result.avg_turns_a = round(a_turn_weight / result.turn_samples_a, 2)
    if result.turn_samples_b:
        result.avg_turns_b = round(b_turn_weight / result.turn_samples_b, 2)
    result.duration_sec = round((time.monotonic() - started), 2)

    # Status precedence: any genuine failure -> failed (wins from completed
    # chunks are still reported); else all-skipped -> skipped; else done.
    if _AB_STATUS_FAILED in statuses:
        result.status = _AB_STATUS_FAILED
    elif statuses and all(s == _AB_STATUS_SKIPPED for s in statuses):
        result.status = _AB_STATUS_SKIPPED
    else:
        result.status = _AB_STATUS_DONE
    if errors:
        result.error = "; ".join(errors)
    return result
