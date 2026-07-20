"""Old-vs-new deck comparison.

Phase 2 prep: when you iterate a deck, you want to know "did this version
actually improve?". Self-play vs opponent pool (run_match) catches absolute
performance, but variance washes out small swap-level deltas. This module runs
both versions at the *same table* with the same fillers — so a card swap
shows up directly as relative wins.

Default mode (recommended for Commander):

    [old_deck, new_deck, filler1, filler2]

Both versions sit in one 4-player pod, facing the same opposition simultaneously.
Cheaper than 1v1 series (1 sim run vs N) and preserves multiplayer dynamics
(political AI, archenemy targeting). Run N games at this single pod composition
for a clean wins-per-version count.

Seat order alternates across pods (even pod index: old in seat 1; odd: new in
seat 1) because Forge keeps seat 1 on the play for every game of an
invocation — same first-player-bias reasoning as
``forge_runner.run_ab_simulation``, which alternates per game. See the
alternation block in ``compare()``.

Filler-pair rotation: with `--filler-pairs 2`, the comparison runs twice with
two different filler pairs from the bracket pool. Counters the bias of any
single filler choice. Aggregates wins across both runs.

Pure 1v1 mode (`--mode 1v1`) uses Forge's constructed format with 2 decks. This
sidesteps the commander 4-deck requirement but gives up multiplayer signal.
Useful for fast efficiency tests (e.g. "does this combo line race the old
combo line"). Singleton/color-identity rules are not enforced by constructed
format — both decks are the same skeleton with swaps, so this is fine, but
the AI's piloting heuristics may differ slightly between formats.

CLI:

    python -m commander_builder.compare_versions \\
        --old "[USER] My Deck v1 [B3].dck" \\
        --new "[USER] My Deck v2 [B3].dck" \\
        --bracket 3 \\
        --games 10 \\
        --filler-pairs 2

Persists `_compare/<old-stem>__vs__<new-stem>_<timestamp>.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import dck_utils
from .forge_runner import ForgeRunner, VENDOR_FORGE
from .game_analyzer import analyze
from .log_parser import _normalize, parse
from .run_match import _fallback_opponents, _load_pool

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
COMPARE_OUT_DIR = DECK_DIR / "_compare"

# Default filler pairs per comparison run. Two pairs averages out single-pair
# bias (an unlucky filler choice that happens to counter one version).
DEFAULT_FILLER_PAIRS = 2


def auto_filler_pairs() -> int:
    """Sprint 1E — pick a filler-pair count that scales with the host's
    CPU. Pods run in parallel (Sprint 1A) so spawning N JVMs on an N-core
    box is roughly the same wall-time as 1 JVM. Bump the default to 4
    on 4+ core systems for tighter verdicts; cap there because extra
    pods past the core count just queue up.

    Bottom-clamped at 2 so single-/dual-core users keep at least one
    extra pod for filler-pair averaging."""
    cores = os.cpu_count() or 2
    return max(2, min(4, cores))
# Default games per pod. Ten is the realistic floor for swap-level signal —
# below that, variance from the other 2 seats drowns the comparison.
DEFAULT_GAMES_PER_POD = 10


@dataclass
class VersionStats:
    """Per-version aggregate across all comparison games."""
    deck_filename: str
    games: int = 0
    wins: int = 0
    avg_ending_life: float = 0.0
    avg_damage_taken: float = 0.0
    avg_turns_when_won: float = 0.0
    avg_turns_when_lost: float = 0.0
    fastest_elimination_turn: Optional[int] = None
    eliminations: int = 0


@dataclass
class ComparisonReport:
    old_deck: str
    new_deck: str
    bracket: int
    timestamp: str
    mode: str                        # "pod" or "1v1"
    games_per_pod: int
    filler_pairs_used: list[list[str]] = field(default_factory=list)
    total_games: int = 0
    draws: int = 0
    old_stats: VersionStats = field(default_factory=lambda: VersionStats(deck_filename=""))
    new_stats: VersionStats = field(default_factory=lambda: VersionStats(deck_filename=""))
    pods: list[dict] = field(default_factory=list)
    card_diff: dict[str, list[str]] = field(default_factory=dict)
    # Sprint 1B — adaptive early-stop. When a pod completes and the
    # cumulative margin is so large that the remaining pods can't
    # possibly flip the verdict, we skip dispatching them. Reports
    # ``stopped_early=True`` and ``pods_completed < len(pods_planned)``.
    # ``pods_planned`` is the number we *would* have run; ``pods``'
    # length is what actually ran.
    stopped_early: bool = False
    pods_planned: int = 0
    # Pod-failure telemetry ("no silent failures"). A pod whose JVM
    # crashed at startup, or that timed out with nothing attributable,
    # is EXCLUDED from total_games / win aggregation — folding its
    # unattributed games in would dilute both versions' win rates
    # toward zero with no warning. These fields let downstream
    # consumers (iteration_loop, web dashboard, _format_summary) see
    # that the verdict rests on fewer games than were requested.
    #   failed_pods       — pods excluded entirely (crash / dead timeout)
    #   timed_out_pods    — pods that hit the watchdog but whose finished
    #                       games WERE salvaged and counted (truncated,
    #                       not discarded)
    #   excluded_games    — per-game results observed in failed pods that
    #                       we threw away because no winner was attributable
    #   pod_failures      — one dict per failed pod: index, decks, reason,
    #                       returncode, timed_out, unattributed_games
    failed_pods: int = 0
    timed_out_pods: int = 0
    excluded_games: int = 0
    pod_failures: list[dict] = field(default_factory=list)
    # Draw-policy label (2026-07-19): turn-cap draws stay counted in
    # ``draws`` and are excluded from decisive stats — never resolved to a
    # surviving life leader the way forge_runner's A/B harness does
    # ('resolve_survivor_leader'). Label only, so downstream analysis can
    # tell the two report populations apart; no behavior change.
    draw_policy: str = "plain_draw"
    # Absorbed-set seat balance (round 3). The odd-pod-count note keys off
    # the PLANNED pod list, but the set of pods that actually contributed
    # games can differ from the plan: parallel early-stop cancels queued
    # pods, sequential early-stop breaks out, and failed pods are excluded
    # outright. Any of those can leave the ABSORBED subset seat-imbalanced
    # with no planned-parity warning (e.g. 4 planned = 2/2, early stop
    # absorbs 3 = 2 old-first / 1 new-first). Counted over non-failed
    # absorbed pods only — a failed pod contributed zero attributed games,
    # so its seat order is irrelevant to the verdict's first-player tilt.
    h2h_seat_balance: dict[str, int] = field(
        default_factory=lambda: {"old_first": 0, "new_first": 0}
    )

    @property
    def winner(self) -> str:
        """Which version won more head-to-head. Reports 'tie' on equal wins."""
        if self.old_stats.wins > self.new_stats.wins:
            return "old"
        if self.new_stats.wins > self.old_stats.wins:
            return "new"
        return "tie"

    @property
    def margin(self) -> int:
        """Absolute win delta (always >= 0)."""
        return abs(self.new_stats.wins - self.old_stats.wins)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["winner"] = self.winner
        d["margin"] = self.margin
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _read_main_section(deck_path: Path) -> list[str]:
    """Return the lines under [Main] in a Forge .dck. Used for card-level diff.

    Thin wrapper over ``dck_utils.iter_section_lines``."""
    if not deck_path.exists():
        return []
    text = deck_path.read_text(encoding="utf-8")
    return list(dck_utils.iter_section_lines(text, "Main"))


def _main_lines_from_text(text: str) -> list[str]:
    """Parse [Main] lines out of a .dck blob string. Same logic as
    `_read_main_section` but takes the raw text instead of a path —
    useful for diffing snapshots stored in knowledge_log without
    materializing temp files.

    Thin wrapper over ``dck_utils.iter_section_lines``."""
    return list(dck_utils.iter_section_lines(text, "Main"))


def diff_decks(old_path: Path, new_path: Path) -> dict[str, list[str]]:
    """Compute the card-level delta between two .dck files.

    Returns `{"added": [...], "removed": [...], "unchanged": [...]}`. Cards are
    keyed by their full Forge line (`<qty> <Name>|<SET>|<CN>`) so quantity and
    printing changes both surface as add+remove pairs."""
    old_lines = set(_read_main_section(old_path))
    new_lines = set(_read_main_section(new_path))
    return {
        "added": sorted(new_lines - old_lines),
        "removed": sorted(old_lines - new_lines),
        "unchanged_count": [str(len(old_lines & new_lines))],
    }


def diff_deck_text(old_text: str, new_text: str) -> dict[str, list[str]]:
    """Same as `diff_decks` but for in-memory .dck blobs. Used by the
    Flask `/api/compare` endpoint to diff iteration snapshots without
    writing temp files."""
    old_lines = set(_main_lines_from_text(old_text))
    new_lines = set(_main_lines_from_text(new_text))
    return {
        "added": sorted(new_lines - old_lines),
        "removed": sorted(old_lines - new_lines),
        "unchanged_count": [str(len(old_lines & new_lines))],
    }


def _pick_filler_pairs(
    bracket: int,
    exclude: list[str],
    num_pairs: int,
) -> list[list[str]]:
    """Pick `num_pairs` distinct pairs of fillers from the curated pool (or
    fallback). Same-bracket only — cross-bracket fillers add noise.

    Strategy: load the candidate pool, exclude the two versions under test,
    then walk the list with stride 2 to spread across the alphabetical / pool
    order. Two pairs from a pool of >= 6 always returns disjoint pairs."""
    candidates = _load_pool(bracket)
    if not candidates:
        # Fallback expects a single exclude string; pass the first version and
        # let the caller's de-dup in-loop drop the second.
        candidates = _fallback_opponents(bracket, exclude=exclude[0], n=num_pairs * 2 + 4)
    # Drop both versions if either snuck into the pool.
    candidates = [c for c in candidates if c not in exclude]
    if len(candidates) < 2:
        raise RuntimeError(
            f"need at least 2 filler candidates at B{bracket}, got {len(candidates)}"
        )
    pairs: list[list[str]] = []
    n = len(candidates)
    # The stride-2 modulo walk below yields n//2 distinct pairs on an
    # even-sized pool; on an odd pool the wrap lands on shifted offsets
    # each lap, so every adjacent (cyclic) pair is eventually produced —
    # n distinct pairs. Beyond that the walk repeats pairs verbatim.
    # Duplicated filler pairs are harmless (they just re-run the same
    # matchup) but they silently reduce opponent diversity, so warn —
    # behavior itself is unchanged.
    distinct_pairs = n if n % 2 else n // 2
    if num_pairs > distinct_pairs:
        print(
            f"WARNING: requested {num_pairs} filler pairs but only "
            f"{distinct_pairs} distinct pair(s) exist in the B{bracket} "
            f"pool ({n} candidates); some pairs will repeat.",
            flush=True,
        )
    for i in range(num_pairs):
        # Stride spreads pair selection. With n=6, num_pairs=2: pairs are
        # (0,1) and (2,3). With n=4, num_pairs=2: pairs are (0,1) and (2,3).
        # Wraps gracefully if num_pairs * 2 > n.
        a = (i * 2) % n
        b = (i * 2 + 1) % n
        if a == b:  # n == 1, degenerate
            b = (b + 1) % n
        pairs.append([candidates[a], candidates[b]])
    return pairs


def _aggregate_pod(
    stats_old: VersionStats,
    stats_new: VersionStats,
    parsed,
    ma,
    old_norm: str,
    new_norm: str,
    won_turns: dict[str, list[int]],
    lost_turns: dict[str, list[int]],
    ending_lives: dict[str, list[int]],
    damages: dict[str, list[int]],
) -> None:
    """Fold one pod's parsed result + match analysis into both VersionStats."""
    # Wins come from log_parser (the authoritative match line).
    for d in parsed.deck_results:
        if d.normalized_name == old_norm:
            stats_old.wins += d.wins
            stats_old.games += parsed.games_completed
        elif d.normalized_name == new_norm:
            stats_new.wins += d.wins
            stats_new.games += parsed.games_completed

    # Per-game telemetry from game_analyzer.
    for game in ma.games:
        for d in game.deck_stats:
            norm = _normalize(d.name)
            if norm == old_norm:
                target = "old"
                stats = stats_old
            elif norm == new_norm:
                target = "new"
                stats = stats_new
            else:
                continue
            if d.ending_life is not None:
                ending_lives[target].append(d.ending_life)
            damages[target].append(d.damage_taken)
            if d.eliminated:
                stats.eliminations += 1
                if d.eliminated_turn is not None:
                    cur = stats.fastest_elimination_turn
                    stats.fastest_elimination_turn = (
                        d.eliminated_turn if cur is None
                        else min(cur, d.eliminated_turn)
                    )
            if game.winner_normalized == norm and game.end_turn is not None:
                won_turns[target].append(game.end_turn)
            elif (
                game.winner_normalized is not None
                and game.winner_normalized != norm
                and game.end_turn is not None
            ):
                lost_turns[target].append(game.end_turn)


def _is_decisive(
    old_wins: int,
    new_wins: int,
    games_remaining: int,
) -> bool:
    """Could ``games_remaining`` more games swing the current margin
    enough to flip or tie the verdict? If not, the current winner is
    locked in and we can stop running pods.

    Examples (games_remaining=10):
      old=11 new=0 → margin 11 > 10 remaining, can't even be tied →
                     decisive (True)
      old=10 new=0 → margin 10: ten straight new-wins ends 10-10, a
                     tie — the winner can still change, so NOT
                     decisive (False)
      old=6  new=4 → margin 2, could swing 10 → not decisive (False)
      old=8  new=2 → margin 6, max swing 10 → not decisive (the
                     remaining games could go 10-0 for new, ending
                     6-12 → margin 6 the other way, so still
                     NOT decisive)

    The conservative rule: decisive iff
      |margin| > games_remaining
    (strictly greater, so a tie remains in play if equal).
    """
    if games_remaining <= 0:
        return True
    margin = abs(new_wins - old_wins)
    return margin > games_remaining


_PER_GAME_WIN_RE = re.compile(
    r"^Game Result:\s*Game\s+(\d+)\s+ended in\s+\d+\s*ms\.\s*"
    r"Ai\((\d+)\)-(.+?)\s+has won!\s*$",
    re.IGNORECASE,
)


def _make_pod_abort_check(
    pod: list[str],
    old_deck: str,
    new_deck: str,
    games_per_pod: int,
):
    """Build the abort-check callback for one pod (Sprint 1C).

    Forge emits one ``Game Result: Game N ended... Ai(X)-Name has won!``
    per game. We parse those incrementally to track old vs new wins
    within this pod; once the in-pod margin exceeds the remaining
    games, the pod's verdict is locked and we kill the JVM. Filler
    decks' wins are tracked so we know when "remaining games in this
    pod" reaches zero, but they don't contribute to the abort margin.

    Returns ``(abort_check, state)`` so the caller can read the final
    counts after the run (used to synthesize a Match Result line if
    Forge was killed before printing its own).

    Note: the abort decision uses only the ABSOLUTE margin
    ``|new_wins - old_wins|``, which is symmetric — so callers may pass
    the head-to-head pair in either order (seat-order alternation puts
    NEW in seat 1 on odd pods). The ``old_wins``/``new_wins`` labels in
    ``state`` then follow the order passed, not the report's old/new;
    nothing downstream reads them for attribution (``wins_by_seat_name``
    carries the synthesis data, keyed by seat+name).
    """
    old_norm = _normalize(old_deck)
    new_norm = _normalize(new_deck)
    state = {
        "old_wins": 0,
        "new_wins": 0,
        # Wins by normalized deck name → seat number map (for synthesizing
        # a Match Result line if we kill the subprocess before Forge prints
        # its own).
        "wins_by_seat_name": {},   # (seat:int, name:str) -> int
        "games_seen": 0,
        "aborted": False,
    }

    def abort_check(line: str) -> bool:
        m = _PER_GAME_WIN_RE.match(line)
        if not m:
            return False
        seat = int(m.group(2))
        name = m.group(3).strip()
        norm = _normalize(name)
        key = (seat, name)
        state["wins_by_seat_name"][key] = state["wins_by_seat_name"].get(key, 0) + 1
        state["games_seen"] += 1
        if norm == old_norm:
            state["old_wins"] += 1
        elif norm == new_norm:
            state["new_wins"] += 1
        # Otherwise: a filler deck won this game; doesn't affect the
        # old-vs-new margin but it is one game further along.
        games_remaining = games_per_pod - state["games_seen"]
        # Only fire when there's still a game we could skip; the
        # last-game case naturally satisfies margin > 0 with
        # games_remaining == 0, but there's nothing left to abort.
        if games_remaining <= 0:
            return False
        margin = abs(state["new_wins"] - state["old_wins"])
        if margin > games_remaining:
            state["aborted"] = True
            return True
        return False

    return abort_check, state


def _synthesize_match_result(state: dict) -> str:
    """When we abort a pod mid-flight, Forge doesn't print its trailing
    ``Match Result:`` summary line that ``log_parser.parse()`` keys on.
    Build one ourselves from the per-game winners we observed so the
    downstream parser sees the same shape it normally would.
    """
    if not state["wins_by_seat_name"]:
        return ""
    # Stable order: sort by seat.
    parts = []
    for (seat, name), wins in sorted(
        state["wins_by_seat_name"].items(), key=lambda kv: kv[0][0],
    ):
        parts.append(f"Ai({seat})-{name}: {wins}")
    return "Match Result: " + " ".join(parts) + "\n"


def _salvage_wins_from_stdout(stdout: str) -> dict:
    """Post-hoc scan of a killed pod's stdout for per-game winner lines.

    Returns the same ``state`` shape ``_make_pod_abort_check`` builds, so
    ``_synthesize_match_result`` can consume it. Used on the TIMEOUT path
    (and by ``run_match``): when the watchdog kills Forge before the
    trailing ``Match Result:`` summary, ``log_parser.parse()`` still
    counts every streamed ``Game Result:`` line into ``games_completed``
    but leaves ``deck_results`` EMPTY — those N finished games would then
    enter the totals with 0 wins for every seat and silently dilute win
    rates. Re-scanning stdout recovers the per-game winners that Forge
    already told us about, exactly like the intra-pod-abort path does
    with its incrementally-built state.
    """
    state: dict = {"wins_by_seat_name": {}, "games_seen": 0}
    for line in stdout.splitlines():
        m = _PER_GAME_WIN_RE.match(line.strip())
        if not m:
            continue
        seat = int(m.group(2))
        name = m.group(3).strip()
        key = (seat, name)
        state["wins_by_seat_name"][key] = state["wins_by_seat_name"].get(key, 0) + 1
        state["games_seen"] += 1
    return state



# Marker substituted for SimResult.forge_log_tail when pods run in
# parallel within the ONE shared Forge profile. The real tail would be
# an interleaved mix of every concurrent pod's log lines, so any stack
# trace in it may belong to a DIFFERENT pod — an honest "unavailable"
# beats confidently-wrong post-mortem data. See _run_one_pod's
# ``shared_profile_parallel`` and the shared-profile comment at
# compare()'s parallel dispatch. Tests assert on this exact string.
_PARALLEL_LOG_TAIL_MARKER = (
    "(forge.log tail unavailable under parallel dispatch — shared profile)"
)


def _run_one_pod(
    runner: ForgeRunner,
    pod: list[str],
    mode: str,
    games_per_pod: int,
    pod_index: int,
    total_pods: int,
    *,
    intra_pod_abort: bool = True,
    shared_profile_parallel: bool = False,
) -> dict:
    """Worker: run one Forge sim, parse, return a structured pod result.

    Pulled out of ``compare()``'s loop so the same code path can serve
    both sequential (``parallel=False``) and threaded execution. MOSTLY
    self-contained — decks and cache are only READ during a sim, so
    concurrent calls don't race on those — but not wholly: Forge appends
    to the shared profile's ``userdata/forge.log`` during every sim, so
    concurrent invocations DO interleave in that one file (see
    ``shared_profile_parallel`` below and the shared-profile comment at
    ``compare()``'s parallel dispatch).

    ``intra_pod_abort`` (Sprint 1C, default True) enables per-game
    abort: as soon as the in-pod margin exceeds the games left in this
    pod, the JVM is killed. We then synthesize the ``Match Result``
    line from observed per-game winners so log_parser can score the
    truncated run.

    ``shared_profile_parallel`` (default False) tells the worker it is
    running concurrently with sibling pods in the SAME Forge profile.
    In that case ``SimResult.forge_log_tail`` — read from the shared
    ``userdata/forge.log`` after the run — is an unattributable
    interleaving of whichever pods happened to be running, so we replace
    it with ``_PARALLEL_LOG_TAIL_MARKER`` rather than hand post-mortems
    another pod's stack trace. Sequential mode keeps the real tail.
    """
    print(
        f"--- Pod {pod_index + 1}/{total_pods} starting: {pod} ---",
        flush=True,
    )
    abort_check = None
    abort_state = None
    if intra_pod_abort and len(pod) >= 2:
        # Seats 1+2 always hold the head-to-head pair, but on odd pods
        # the seat-order alternation puts them in [new, old] order — so
        # pod[0] is NOT necessarily the old deck. The abort math only
        # uses the ABSOLUTE margin |a - b| (symmetric), so passing the
        # pair in seat order is correct regardless of orientation.
        h2h_a, h2h_b = pod[0], pod[1]
        abort_check, abort_state = _make_pod_abort_check(
            pod, h2h_a, h2h_b, games_per_pod,
        )
    if mode == "1v1":
        result = runner.run(
            pod, num_games=games_per_pod, game_format="constructed",
            abort_check=abort_check,
        )
    else:
        result = runner.run(
            pod, num_games=games_per_pod, abort_check=abort_check,
        )
    if shared_profile_parallel:
        # Every parallel pod shares ONE profile, so forge.log is a single
        # file all the concurrent JVMs append to; the tail read after
        # THIS pod finished is an interleaving of whichever pods were
        # running and can't be attributed to this one. Replace it with an
        # explicit marker so a post-mortem sees "unavailable" instead of
        # (very plausibly) another pod's crash.
        result.forge_log_tail = _PARALLEL_LOG_TAIL_MARKER
    stdout = result.stdout
    pod_aborted = bool(abort_state and abort_state.get("aborted"))
    timeout_salvaged = False
    if pod_aborted and "Match Result:" not in stdout:
        # The abort killed Forge before its trailing summary fired.
        # Stitch in a synthetic one so log_parser can attribute wins.
        stdout = stdout + _synthesize_match_result(abort_state)
    elif result.timed_out and "Match Result:" not in stdout:
        # TIMEOUT path — mirror the abort path above. The watchdog killed
        # Forge before the trailing summary, but any game that DID finish
        # already printed its "Game Result: ... has won!" line. Without
        # synthesis those N games would land in totals as games_completed=N
        # with deck_results=[] — 0 wins for both versions, silently
        # diluting the comparison. Salvage what actually finished.
        salvaged = _salvage_wins_from_stdout(stdout)
        if salvaged["wins_by_seat_name"]:
            stdout = stdout + _synthesize_match_result(salvaged)
            timeout_salvaged = True
    parsed = parse(stdout)
    ma = analyze(stdout)

    # Failure classification ("no silent failures"). SimResult never
    # raises — it CARRIES the failure in error/returncode/timed_out and
    # it is on us, the consumer, to inspect them. Rules:
    #   - error without timeout        → subprocess never ran / blew up
    #   - nonzero exit (not our abort  → JVM crashed on its own; a real
    #     kill, not a timeout)           crash is not salvageable data
    #     (same policy as forge_runner's A/B path: don't rescue crashes)
    #   - timed out with NOTHING       → pod hung before any game
    #     attributable                   finished; nothing to count
    # A nonzero returncode caused by OUR intra-pod-abort kill is expected
    # and is NOT a failure — the synthesized Match Result covers it.
    pod_failed = False
    failure_reason: Optional[str] = None
    if result.error and not result.timed_out:
        pod_failed = True
        failure_reason = result.error
    elif (
        result.returncode not in (0, None)
        and not pod_aborted
        and not result.timed_out
    ):
        pod_failed = True
        failure_reason = f"Forge exited with code {result.returncode}"
    elif result.timed_out and not parsed.deck_results:
        pod_failed = True
        failure_reason = result.error or "timed out with no attributable game results"

    print(
        f"--- Pod {pod_index + 1}/{total_pods} done in "
        f"{result.duration_sec:.1f}s"
        + (f" (intra-pod abort, {abort_state['games_seen']}/{games_per_pod} games)"
           if pod_aborted else "")
        + (f" (FAILED: {failure_reason})" if pod_failed else "")
        + " ---",
        flush=True,
    )
    return {
        "pod_index": pod_index,
        "pod": pod,
        "duration_sec": round(result.duration_sec, 1),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "error": result.error,
        "intra_pod_aborted": pod_aborted,
        # Failure surface for the aggregator + persisted report: the
        # aggregator uses pod_failed to EXCLUDE this pod's games, and the
        # JSON keeps the reason so a post-mortem doesn't need the console.
        "pod_failed": pod_failed,
        "failure_reason": failure_reason,
        "timeout_salvaged": timeout_salvaged,
        "games_actually_played": (
            abort_state["games_seen"] if abort_state else games_per_pod
        ),
        "match": ma.to_dict(),
        # Keep parsed + analyzer outputs around so the parent can run
        # the per-pod aggregator without re-parsing.
        "_parsed": parsed,
        "_analyzed": ma,
    }


def compare(
    old_deck: str,
    new_deck: str,
    bracket: int,
    games_per_pod: int = DEFAULT_GAMES_PER_POD,
    filler_pairs: int = DEFAULT_FILLER_PAIRS,
    mode: str = "pod",
    runner: Optional[ForgeRunner] = None,
    out_dir: Path = COMPARE_OUT_DIR,
    deck_dir: Optional[Path] = None,
    parallel: bool = True,
    max_workers: Optional[int] = None,
    early_stop: bool = True,
    seat_parity: int = 0,
    suppress_seat_note: bool = False,
) -> ComparisonReport:
    """Run the head-to-head comparison and persist the report.

    ``deck_dir`` overrides the default Commander ``DECK_DIR`` for path
    resolution. Use it when the staged files live elsewhere (e.g. the
    web endpoint's 1v1 mode stages converted decks under
    ``userdata/decks/constructed/`` so Forge's ``-f constructed``
    can find them).

    ``parallel`` (default True) dispatches multi-pod runs concurrently.
    Pods are I/O-bound (each spawns a Forge JVM as a subprocess) so a
    threaded executor is sufficient; the GIL is released while the
    subprocess executes. With 2 filler pairs on a 4-core machine,
    expect ~2× wall-clock improvement on pod-mode A/B sims. Pass
    ``parallel=False`` to force the sequential path (used by tests
    that need deterministic logging or by callers that want stable
    progress output).

    ``max_workers`` caps the threadpool size; defaults to
    ``min(len(pods), os.cpu_count() or 4)`` which avoids spawning more
    JVMs than there are CPU cores.

    ``early_stop`` (default True) skips remaining pods once the
    cumulative margin is too large for them to possibly flip the
    verdict (see ``_is_decisive``). Under parallel dispatch only
    NOT-YET-STARTED pods are skipped; pods already in flight run to
    completion (their JVM cost is paid either way) and their games ARE
    absorbed into the report. With the default 2 filler pairs
    the saving is small because both pods already run in parallel;
    the value grows once ``filler_pairs`` is bumped above the core
    count or sequential mode is forced.

    ``seat_parity`` (default 0) shifts the seat-order alternation
    phase: pods whose ``(index + seat_parity)`` is even seat OLD in
    seat 1, odd pods seat NEW in seat 1 (see the alternation block
    below). Callers that run SEVERAL compare() calls with a single pod
    each (meta_test with ``filler_pairs=1``) pass their own call index
    here so the head-to-head seat-1 share still balances across the
    whole batch even though no single call has two pods to alternate.

    ``suppress_seat_note`` (default False) silences the two seat-balance
    console notes (odd-pod-count residual, absorbed-set imbalance) WITHOUT
    touching the report fields. For a direct compare() the notes are the
    only surface a human sees, so they stay on by default. Batch drivers
    that make many single-pod calls (meta_test with filler_pairs=1) would
    otherwise print the odd-pod note once per call — pure spam, since each
    call's "residual" is by design and cancels across the batch. Those
    callers pass True and print ONE aggregate line themselves from the
    ``h2h_seat_balance`` fields.
    """
    if mode not in {"pod", "1v1"}:
        raise ValueError(f"mode must be 'pod' or '1v1', got {mode!r}")
    runner = runner or ForgeRunner.locate()

    resolved_deck_dir = deck_dir or DECK_DIR
    old_path = resolved_deck_dir / old_deck
    new_path = resolved_deck_dir / new_deck
    if not old_path.exists():
        raise FileNotFoundError(f"old deck not found: {old_deck}")
    if not new_path.exists():
        raise FileNotFoundError(f"new deck not found: {new_deck}")
    if old_deck == new_deck:
        raise ValueError("old and new must be different decks")

    old_norm = _normalize(old_deck)
    new_norm = _normalize(new_deck)

    report = ComparisonReport(
        old_deck=old_deck,
        new_deck=new_deck,
        bracket=bracket,
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode=mode,
        games_per_pod=games_per_pod,
        old_stats=VersionStats(deck_filename=old_deck),
        new_stats=VersionStats(deck_filename=new_deck),
    )
    report.card_diff = diff_decks(old_path, new_path)

    won_turns: dict[str, list[int]] = {"old": [], "new": []}
    lost_turns: dict[str, list[int]] = {"old": [], "new": []}
    ending_lives: dict[str, list[int]] = {"old": [], "new": []}
    damages: dict[str, list[int]] = {"old": [], "new": []}

    if mode == "1v1":
        pods = [[old_deck, new_deck]]
        report.filler_pairs_used = []
    else:
        pairs = _pick_filler_pairs(bracket, exclude=[old_deck, new_deck], num_pairs=filler_pairs)
        report.filler_pairs_used = pairs
        pods = [[old_deck, new_deck, *pair] for pair in pairs]

    # Seat-order alternation — first-player-bias fix. Forge does NOT
    # randomize turn order by seat: seat 1 is on the play for every game
    # of an invocation. forge_runner.run_ab_simulation established the
    # precedent — it explicitly alternates the head-to-head pair per game
    # ("Alternate seat order — even iters: A first; odd iters: B first")
    # for exactly this reason. compare() can't do per-game alternation
    # because all games_per_pod games of a pod share ONE Forge invocation
    # with one fixed seat order, so pod-level parity is the correct
    # granularity here: even (index + seat_parity) seats OLD first, odd
    # seats NEW first. With an even pod count the first-player advantage
    # cancels exactly. The filler pair stays in seats 3+4 either way,
    # matching run_ab_simulation ("The filler pair stays in seats 3+4 in
    # both cases; only the head-to-head pair flips").
    #
    # Without this, the old deck sat in seat 1 for EVERY game of EVERY
    # pod, tilting every compare()-based verdict (iteration_loop, web
    # propose_swap) toward "reverted" by pure turn-order advantage.
    #
    # Attribution is unaffected by the flip: _aggregate_pod keys on
    # normalized deck NAMES from the Match Result / Game Result lines,
    # never on seat number. We flip the pod lists in place BEFORE
    # dispatch so every downstream record of the pod (the runner call,
    # pr["pod"], report.pods, pod_failures) reads the true seat order.
    for i, pod in enumerate(pods):
        if (i + seat_parity) % 2 == 1:
            pod[0], pod[1] = pod[1], pod[0]
    if len(pods) % 2 == 1 and not suppress_seat_note:
        # Odd pod count: alternation can't cancel exactly — one side gets
        # ceil(n/2) seat-1 pods, the other floor(n/2). Surface the
        # residual so a razor-thin verdict can be read with that grain
        # of salt (a single pod, e.g. 1v1 or filler_pairs=1, is the
        # degenerate case: the noted deck holds seat 1 for the whole run).
        # Batch drivers that intentionally make many single-pod calls
        # (meta_test) suppress this per-call note and print one aggregate
        # line instead — see the suppress_seat_note docstring.
        extra_label, extra_deck = (
            ("OLD", old_deck) if seat_parity % 2 == 0 else ("NEW", new_deck)
        )
        print(
            f"NOTE: odd pod count ({len(pods)}) — seat-order alternation "
            f"leaves {extra_label} ({extra_deck}) with the extra seat-1 "
            f"(first-player) pod.",
            flush=True,
        )

    # Dispatch pods. With parallel=True we run all pods concurrently in
    # a threadpool — each one spawns its own Forge subprocess so the
    # GIL doesn't bottleneck. With parallel=False (or len(pods)==1) we
    # run sequentially for deterministic logging / test stability.
    #
    # Aggregation happens as each pod completes (instead of in a
    # second pass after all pods finish) so the early-stop check can
    # see cumulative wins after every pod and skip remaining work.
    report.pods_planned = len(pods)
    completed_pods: dict[int, dict] = {}
    use_parallel = parallel and len(pods) > 1

    def _absorb(pr: dict) -> None:
        """Apply one pod result to the cumulative report. Returns nothing
        — mutates `report`, `won_turns`, `lost_turns`, `ending_lives`,
        `damages` in place."""
        parsed = pr.pop("_parsed")
        ma = pr.pop("_analyzed")
        pr["pod_index"] = pr["pod_index"] + 1   # display is 1-based
        # Seat-order provenance: pr["pod"] already lists the TRUE seat
        # order (pods are flipped in place before dispatch), but make
        # the head-to-head orientation explicit so post-mortems don't
        # have to compare pod[0] against the report header to know who
        # was on the play.
        pr["h2h_seat_order"] = (
            "new_first" if pr["pod"] and pr["pod"][0] == new_deck else "old_first"
        )
        completed_pods[pr["pod_index"] - 1] = pr
        if pr["pod_failed"]:
            # No silent failures: a crashed or dead-timed-out pod must not
            # contribute to total_games. parse() counts per-game lines into
            # games_completed even when no Match Result attributed a winner
            # (deck_results=[] → draws property reads 0), so folding the pod
            # in would add N games with 0 wins for BOTH versions — silently
            # dragging both win rates toward zero. Exclude it, record the
            # failure on the report, and warn loudly.
            report.failed_pods += 1
            report.excluded_games += parsed.games_completed
            report.pod_failures.append({
                "pod_index": pr["pod_index"],
                "pod": pr["pod"],
                "reason": pr["failure_reason"],
                "returncode": pr["returncode"],
                "timed_out": pr["timed_out"],
                "unattributed_games": parsed.games_completed,
            })
            print(
                f"WARNING: pod {pr['pod_index']}/{len(pods)} FAILED and is "
                f"EXCLUDED from the comparison: {pr['failure_reason']} "
                f"({parsed.games_completed} unattributed game(s) discarded).",
                flush=True,
            )
            return
        if pr["timed_out"]:
            # The pod hit the watchdog but finished games WERE attributed
            # (either a synthesized Match Result or Forge's own trailing
            # line made it out). Those results are real — count them — but
            # flag the truncation so the verdict isn't mistaken for a
            # full-length run.
            report.timed_out_pods += 1
            print(
                f"WARNING: pod {pr['pod_index']}/{len(pods)} TIMED OUT "
                f"mid-run; salvaged {parsed.games_completed} finished "
                f"game(s) of {games_per_pod} requested.",
                flush=True,
            )
        report.total_games += parsed.games_completed
        report.draws += parsed.draws
        _aggregate_pod(
            report.old_stats, report.new_stats,
            parsed, ma, old_norm, new_norm,
            won_turns, lost_turns, ending_lives, damages,
        )

    def _check_early_stop() -> bool:
        """True if the verdict is locked in and no remaining pod could
        flip it. Skipped on the first pod to avoid early-stopping on
        a single noisy result."""
        if not early_stop:
            return False
        if len(completed_pods) <= 1:
            return False
        remaining = len(pods) - len(completed_pods)
        games_remaining = remaining * games_per_pod
        return _is_decisive(
            report.old_stats.wins, report.new_stats.wins, games_remaining,
        )

    if use_parallel:
        workers = max_workers or min(len(pods), os.cpu_count() or 4)
        print(
            f"\n--- Dispatching {len(pods)} pods in parallel "
            f"(workers={workers}) ---",
            flush=True,
        )
        # SHARED-PROFILE PARALLELISM — reconciling with forge_runner's own
        # warnings. run_ab_batch / run_ab_parallel refuse to let two Forge
        # instances share a profile ("they'd collide on the deck dir,
        # cache, and forge.log" — ForgeRunner.for_profile) and hand each
        # worker its own cwd-isolated vendor/forge{N} profile. This
        # dispatch DOES run every pod concurrently in the one shared
        # profile. Taking the three collision surfaces honestly:
        #   - deck dir: safe HERE. Those helpers' callers stage/copy decks
        #     into the profile as part of each job; compare() writes
        #     nothing during dispatch — every pod only READS .dck files
        #     that already exist before the first submit (old/new are
        #     verified above; web callers stage before calling). Concurrent
        #     readers don't collide.
        #   - cache: read-mostly in headless sim (the card DB/pics were
        #     populated long before any compare() runs). A cold, never-run
        #     profile could race on first-time cache population; accepted
        #     risk, since every deployed profile is warmed.
        #   - forge.log: the collision is REAL and unavoidable — every JVM
        #     appends to the one userdata/forge.log, so a per-pod tail read
        #     is an unattributable interleaving. We don't pretend
        #     otherwise: _run_one_pod(shared_profile_parallel=True)
        #     replaces forge_log_tail with an explicit marker.
        # The robust alternative is run_ab_parallel's per-worker profiles
        # (vendor/forge2..N); adopting it here is deliberately out of
        # scope — it would change profile discovery and deck staging for
        # every compare() caller.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(
                    _run_one_pod, runner, pod, mode, games_per_pod, i, len(pods),
                    shared_profile_parallel=True,
                ): i
                for i, pod in enumerate(pods)
            }
            # `stop_decided` latches the first decisive verdict so late
            # absorptions can't re-trigger (or un-trigger) the early-stop
            # decision: after the cancel sweep every future is done,
            # running, or cancelled — there is nothing left to cancel, and
            # nothing restarts a cancelled future.
            stop_decided = False
            for fut in as_completed(futures):
                if fut.cancelled():
                    # Cancelled == never started (Future.cancel() only
                    # succeeds on not-yet-running futures), so there is no
                    # result to absorb — and .result() would raise
                    # CancelledError. Skipping here can't misalign the
                    # report: _absorb keys completed_pods on each result's
                    # OWN pod_index, so absent indices simply don't appear.
                    continue
                _absorb(fut.result())
                if stop_decided or not _check_early_stop():
                    continue
                # Verdict locked in. Cancel any still-QUEUED pods —
                # already-running pods can't be interrupted (the Forge
                # subprocess won't honor a thread cancel), so their full
                # wall-clock cost is paid regardless of what we do next.
                # That is exactly why we do NOT `break` here: the old
                # break-out still blocked on the executor's shutdown join
                # for as long as the in-flight JVMs took, but silently
                # threw their fully-played games away. Keep draining
                # as_completed instead and absorb in-flight pods through
                # the same _absorb path (same failure classification, same
                # seat/pod-index bookkeeping) when they land.
                stop_decided = True
                canceled = sum(1 for f in futures if f.cancel())
                if canceled:
                    report.stopped_early = True
                    print(
                        f"--- Early stop: verdict locked in, canceled "
                        f"{canceled} pending pod(s); draining pods already "
                        f"in flight ---",
                        flush=True,
                    )
    else:
        for i, pod in enumerate(pods):
            _absorb(
                _run_one_pod(runner, pod, mode, games_per_pod, i, len(pods)),
            )
            remaining = len(pods) - len(completed_pods)
            # Only flag early-stop when there are still pods we can
            # actually skip; otherwise the decisive check just
            # confirms we ran the planned number.
            if remaining > 0 and _check_early_stop():
                report.stopped_early = True
                print(
                    f"--- Early stop: verdict locked in, "
                    f"skipped {remaining} remaining pod(s) ---",
                    flush=True,
                )
                break

    # Order completed pods by their original index so the report's
    # `pods` list lines up with `filler_pairs_used`.
    for idx in sorted(completed_pods):
        report.pods.append(completed_pods[idx])

    # Absorbed-set seat balance. The odd-pod-count note above reasons
    # about the PLANNED pod list; the pods that actually fed the verdict
    # can be a different set — early-stop (parallel cancel or sequential
    # break) drops trailing pods and failed pods are excluded — so an
    # even PLAN can still yield an imbalanced ABSORBED set with no
    # warning. Count the head-to-head orientation of every non-failed
    # absorbed pod (h2h_seat_order is stamped in _absorb from the true,
    # already-flipped seat list) and surface the residual when it can
    # actually color the verdict.
    for pr in report.pods:
        if not pr["pod_failed"]:
            report.h2h_seat_balance[pr["h2h_seat_order"]] += 1
    bal_old = report.h2h_seat_balance["old_first"]
    bal_new = report.h2h_seat_balance["new_first"]
    # Planned split under the same parity, for the "differs from plan"
    # check: even (i + seat_parity) pods seat OLD first (the alternation
    # loop above), so the plan gives OLD first in ceil-or-floor(n/2) pods
    # depending on the phase.
    planned_old = sum(
        1 for i in range(report.pods_planned) if (i + seat_parity) % 2 == 0
    )
    planned_new = report.pods_planned - planned_old
    # Two triggers, matching how the imbalance arises:
    #   |old - new| > 1  — a residual of 1 is unavoidable for any odd
    #                      absorbed count and (when planned) is already
    #                      covered by the odd-pod-count note; >1 means
    #                      one side got at least two extra on-the-play
    #                      pods, which alternation can never produce.
    #   absorbed != plan — early-stop/failures changed the set, so the
    #                      planned-parity reasoning (and the note above,
    #                      or its silence) no longer describes reality.
    # (bal_old or bal_new) guard: when EVERY pod failed nothing was
    # absorbed at all — the verdict is already loudly flagged as built on
    # zero games, and a "0/0 seat split" note would be noise on top.
    if not suppress_seat_note and (bal_old or bal_new) and (
        abs(bal_old - bal_new) > 1
        or (bal_old, bal_new) != (planned_old, planned_new)
    ):
        print(
            f"NOTE: absorbed-pod seat balance — OLD on the play in "
            f"{bal_old} pod(s), NEW in {bal_new} (planned "
            f"{planned_old}/{planned_new}); early stop and/or excluded "
            f"pods changed the seat-1 split, so read a razor-thin margin "
            f"with the first-player residual in mind.",
            flush=True,
        )

    # Finalize derived averages.
    for stats, target in [(report.old_stats, "old"), (report.new_stats, "new")]:
        if ending_lives[target]:
            stats.avg_ending_life = round(sum(ending_lives[target]) / len(ending_lives[target]), 1)
        if damages[target]:
            stats.avg_damage_taken = round(sum(damages[target]) / len(damages[target]), 1)
        if won_turns[target]:
            stats.avg_turns_when_won = round(sum(won_turns[target]) / len(won_turns[target]), 1)
        if lost_turns[target]:
            stats.avg_turns_when_lost = round(sum(lost_turns[target]) / len(lost_turns[target]), 1)

    # Surface pod failures one more time at the end — per-pod warnings can
    # scroll away under parallel pod output, and this is the last thing the
    # user reads before the verdict line.
    if report.failed_pods:
        print(
            f"\nWARNING: {report.failed_pods}/{report.pods_planned or len(report.pods)} "
            f"pod(s) failed and were excluded "
            f"({report.excluded_games} unattributed game(s) discarded). "
            f"The verdict is based on {report.total_games} attributed game(s) only.",
            flush=True,
        )

    # Persist.
    out_dir.mkdir(parents=True, exist_ok=True)
    old_stem = re.sub(r"[^\w-]+", "_", Path(old_deck).stem).strip("_") or "old"
    new_stem = re.sub(r"[^\w-]+", "_", Path(new_deck).stem).strip("_") or "new"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{old_stem}__vs__{new_stem}_B{bracket}_{ts}.json"
    out_path.write_text(report.to_json(), encoding="utf-8")
    print(f"\nWrote comparison report: {out_path}", flush=True)
    return report


def _format_summary(report: ComparisonReport) -> str:
    lines = []
    lines.append(f"=== Comparison: B{report.bracket} | mode={report.mode} ===")
    lines.append(f"Old: {report.old_deck}")
    lines.append(f"New: {report.new_deck}")
    lines.append(f"Pods: {len(report.pods)} × {report.games_per_pod} games = {report.total_games} total ({report.draws} draws)")
    lines.append(
        f"Head-to-head: OLD {report.old_stats.wins} - {report.new_stats.wins} NEW  "
        f"(winner: {report.winner.upper()}, margin {report.margin})"
    )
    # Failure telemetry belongs right under the verdict line — a verdict
    # built on half the requested pods reads very differently.
    if report.failed_pods or report.timed_out_pods:
        lines.append(
            f"!! {report.failed_pods} failed pod(s) EXCLUDED "
            f"({report.excluded_games} unattributed game(s) discarded), "
            f"{report.timed_out_pods} timed-out pod(s) truncated."
        )
        for f in report.pod_failures:
            lines.append(f"!!   pod {f['pod_index']}: {f['reason']}")
    lines.append("")
    lines.append("Per-version detail:")
    for label, stats in [("old", report.old_stats), ("new", report.new_stats)]:
        lines.append(f"  {label}: {stats.wins}W / {stats.eliminations}E / "
                     f"avg_life={stats.avg_ending_life}, avg_dmg={stats.avg_damage_taken}, "
                     f"avg_turns_won={stats.avg_turns_when_won}, "
                     f"avg_turns_lost={stats.avg_turns_when_lost}, "
                     f"fastest_elim={stats.fastest_elimination_turn}")
    if report.card_diff.get("added") or report.card_diff.get("removed"):
        lines.append("")
        lines.append("Card delta:")
        for c in report.card_diff.get("removed", []):
            lines.append(f"  - {c}")
        for c in report.card_diff.get("added", []):
            lines.append(f"  + {c}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="compare_versions")
    p.add_argument("--old", required=True, help="Filename of the OLD deck version (.dck under commander/).")
    p.add_argument("--new", required=True, help="Filename of the NEW deck version.")
    p.add_argument("--bracket", type=int, required=True)
    p.add_argument("--games", type=int, default=DEFAULT_GAMES_PER_POD,
                   help=f"Games per pod (default {DEFAULT_GAMES_PER_POD}).")
    p.add_argument("--filler-pairs", type=int, default=DEFAULT_FILLER_PAIRS,
                   help=f"Number of filler-pair pods to run (default {DEFAULT_FILLER_PAIRS}).")
    p.add_argument("--mode", choices=["pod", "1v1"], default="pod",
                   help="'pod' = 4-player same-table comparison (default); '1v1' = Forge constructed 2-deck.")
    args = p.parse_args(argv)

    report = compare(
        old_deck=args.old,
        new_deck=args.new,
        bracket=args.bracket,
        games_per_pod=args.games,
        filler_pairs=args.filler_pairs,
        mode=args.mode,
    )
    summary = _format_summary(report)
    try:
        print("\n" + summary)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(("\n" + summary + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
