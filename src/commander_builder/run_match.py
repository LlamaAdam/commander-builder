"""User-deck matchup orchestrator.

Layer 2 of the testing strategy (see PROJECT.md). Where `pool_curator.py` builds
a canonical opponent pool by self-play, this module pits a *user* deck against
that pool and produces a weakness report.

Workflow:

    python -m commander_builder.run_match \\
        --user "[USER] Hakbal of the Surging Soul [B3].dck" \\
        --bracket 3 --games 3 --pods 2

  1. Load the curated pool for the bracket (or fall back to the first N
     opponents on disk if no pool exists yet).
  2. Build pods: user deck + 3 opponents per pod.
  3. Run each pod, capture stdout, log_parser + game_analyzer extract.
  4. Aggregate user-deck performance + per-game narrative.
  5. Write `_matches/<deck-stem>_B<n>_<timestamp>.json`.

The report focuses on signals an actual deck-tuning pass would care about:

  - Win rate against the pool (vs vs each opponent if pods rotate).
  - Average turns survived when losing (early elimination = consistency hole).
  - Average ending life when winning (was the win comfortable or skin-of-teeth).
  - Damage taken per game (does this deck just race or does it stabilize?).
  - confirmAction count (proxy for "AI couldn't pilot this card").
  - Unsupported cards (concrete swap candidates).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .forge_runner import ForgeRunner, VENDOR_FORGE
from .game_analyzer import MatchAnalysis, analyze
from .log_parser import _normalize, parse

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
POOL_DIR = DECK_DIR / "_pools"
MATCH_OUT_DIR = DECK_DIR / "_matches"


@dataclass
class MatchupReport:
    user_deck: str
    bracket: int
    timestamp: str
    games_played: int = 0
    user_wins: int = 0
    user_losses: int = 0
    draws: int = 0
    avg_user_ending_life: float = 0.0
    avg_user_damage_taken: float = 0.0
    avg_turns_when_won: float = 0.0
    avg_turns_when_lost: float = 0.0
    fastest_loss_turn: Optional[int] = None
    confirm_action_count: int = 0
    unsupported_cards: list[str] = field(default_factory=list)
    per_opponent_record: dict[str, dict] = field(default_factory=dict)
    pods: list[dict] = field(default_factory=list)
    # Pod-failure telemetry ("no silent failures"). Failed pods — JVM
    # crash, or timeout with nothing attributable — are EXCLUDED from
    # games_played, so their unattributed games can never be counted as
    # phantom user LOSSES (user_losses = decisive - wins would otherwise
    # book every unattributed game as a loss). Mirrors the fields on
    # compare_versions.ComparisonReport.
    failed_pods: int = 0
    timed_out_pods: int = 0
    excluded_games: int = 0
    pod_failures: list[dict] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        decisive = self.games_played - self.draws
        return self.user_wins / decisive if decisive > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"] = round(self.win_rate, 3)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _load_pool(bracket: int, pool_dir: Path = POOL_DIR) -> list[str]:
    """Return Pool A + Pool B from the curated pool JSON, or [] if absent."""
    pool_path = pool_dir / f"B{bracket}.json"
    if not pool_path.exists():
        return []
    data = json.loads(pool_path.read_text(encoding="utf-8"))
    return list(data.get("pool_a", [])) + list(data.get("pool_b", []))


def _fallback_opponents(bracket: int, exclude: str, n: int) -> list[str]:
    """No curated pool yet — sample N opponents at the bracket alphabetically.
    Excludes the user deck and any other [USER]-prefixed deck so a stale leftover
    can't sneak in as 'opposition'."""
    suffix = f" [B{bracket}].dck"
    out: list[str] = []
    for path in sorted(DECK_DIR.glob("*.dck")):
        name = path.name
        if not name.endswith(suffix):
            continue
        if name == exclude or name.startswith("[USER]"):
            continue
        out.append(name)
        if len(out) >= n:
            break
    return out


def _build_pods(user_deck: str, opponents: list[str], num_pods: int) -> list[list[str]]:
    """Build `num_pods` pods of 4 (user + 3 opponents). Rotates opponents so
    different opponents see the user across pods. With 6 opponents and 2 pods,
    each opponent appears in exactly 1 pod."""
    if len(opponents) < 3:
        raise ValueError(f"need at least 3 opponents, got {len(opponents)}")
    pods: list[list[str]] = []
    for i in range(num_pods):
        # Stride through opponents, wrapping around. Avoids picking the same
        # 3 opponents every pod when len(opponents) < 3 * num_pods.
        slot = (i * 3) % len(opponents)
        trio = []
        cur = slot
        while len(trio) < 3:
            opp = opponents[cur % len(opponents)]
            if opp not in trio:
                trio.append(opp)
            cur += 1
        pods.append([user_deck, *trio])
    return pods


def run_matchup(
    user_deck: str,
    bracket: int,
    games_per_pod: int = 3,
    num_pods: int = 2,
    runner: Optional[ForgeRunner] = None,
    out_dir: Path = MATCH_OUT_DIR,
) -> MatchupReport:
    runner = runner or ForgeRunner.locate()

    # Verify the user deck exists on disk before booting the JVM.
    if not (DECK_DIR / user_deck).exists():
        raise FileNotFoundError(f"user deck not found: {user_deck}")

    pool = _load_pool(bracket)
    if not pool:
        print(f"  No curated pool for B{bracket}; falling back to first {num_pods * 3} on-disk opponents.")
        pool = _fallback_opponents(bracket, exclude=user_deck, n=num_pods * 3)
    if len(pool) < 3:
        raise RuntimeError(f"not enough B{bracket} opponents available (got {len(pool)})")

    pods = _build_pods(user_deck, pool, num_pods)
    user_norm = _normalize(user_deck)

    report = MatchupReport(
        user_deck=user_deck,
        bracket=bracket,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    won_turns: list[int] = []
    lost_turns: list[int] = []
    ending_lives: list[int] = []
    damages: list[int] = []

    for i, pod in enumerate(pods):
        print(f"\n--- Pod {i + 1}/{len(pods)}: {pod} ---", flush=True)
        result = runner.run(pod, num_games=games_per_pod)
        stdout = result.stdout
        if result.timed_out and "Match Result:" not in stdout:
            # The watchdog killed Forge before the trailing "Match Result:"
            # summary. parse() would still count the per-game "Game Result"
            # lines into games_completed but attribute NO winners — and the
            # user_losses = decisive - wins arithmetic below would book
            # every one of those games as a user LOSS. Salvage the games
            # that actually finished by re-scanning stdout for per-game
            # winner lines (same policy as compare_versions' timeout path).
            # Lazy import: compare_versions imports _load_pool /
            # _fallback_opponents from THIS module at import time, so a
            # top-level import here would be circular.
            from .compare_versions import (
                _salvage_wins_from_stdout,
                _synthesize_match_result,
            )
            salvaged = _salvage_wins_from_stdout(stdout)
            if salvaged["wins_by_seat_name"]:
                stdout = stdout + _synthesize_match_result(salvaged)
        parsed = parse(stdout)
        ma = analyze(stdout)

        # Failure classification — same rules as compare_versions'
        # _run_one_pod (see the why-comment there): crashes are never
        # salvaged (matching forge_runner's A/B policy), timeouts count
        # only if at least one game was attributable.
        pod_failed = False
        failure_reason: Optional[str] = None
        if result.error and not result.timed_out:
            pod_failed = True
            failure_reason = result.error
        elif result.returncode not in (0, None) and not result.timed_out:
            pod_failed = True
            failure_reason = f"Forge exited with code {result.returncode}"
        elif result.timed_out and not parsed.deck_results:
            pod_failed = True
            failure_reason = result.error or "timed out with no attributable game results"

        report.pods.append({
            "pod_index": i + 1,
            "pod": pod,
            "duration_sec": round(result.duration_sec, 1),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "error": result.error,
            "pod_failed": pod_failed,
            "failure_reason": failure_reason,
            "match": ma.to_dict(),
        })

        if pod_failed:
            # Excluded, not counted: adding this pod's unattributed games
            # to games_played would inflate user_losses with games nobody
            # verifiably won. Surface it loudly instead.
            report.failed_pods += 1
            report.excluded_games += parsed.games_completed
            report.pod_failures.append({
                "pod_index": i + 1,
                "pod": pod,
                "reason": failure_reason,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "unattributed_games": parsed.games_completed,
            })
            print(
                f"WARNING: pod {i + 1}/{len(pods)} FAILED and is EXCLUDED "
                f"from the matchup: {failure_reason} "
                f"({parsed.games_completed} unattributed game(s) discarded).",
                flush=True,
            )
            continue
        if result.timed_out:
            # Truncated but attributable — count the finished games, flag
            # the truncation so the record isn't read as a full-length run.
            report.timed_out_pods += 1
            print(
                f"WARNING: pod {i + 1}/{len(pods)} TIMED OUT mid-run; "
                f"salvaged {parsed.games_completed} finished game(s) of "
                f"{games_per_pod} requested.",
                flush=True,
            )

        # Aggregate user wins from log_parser (the authoritative match line).
        for d in parsed.deck_results:
            if d.normalized_name == user_norm:
                report.user_wins += d.wins

        report.games_played += parsed.games_completed
        report.draws += parsed.draws
        report.confirm_action_count += len(parsed.confirm_action_cards)
        report.unsupported_cards.extend(parsed.unsupported_cards)

        # Per-game user telemetry from game_analyzer.
        for game in ma.games:
            user_stats = next(
                (d for d in game.deck_stats if _normalize(d.name) == user_norm),
                None,
            )
            if user_stats is None:
                continue
            if user_stats.ending_life is not None:
                ending_lives.append(user_stats.ending_life)
            damages.append(user_stats.damage_taken)
            if game.winner_normalized == user_norm and game.end_turn is not None:
                won_turns.append(game.end_turn)
            # `is not None` guard: a drawn game has winner_normalized None,
            # which satisfies `!= user_norm` — without the guard the draw's
            # end_turn (typically the turn cap, i.e. huge) was booked as a
            # LOSS turn and inflated avg_turns_when_lost. Draws are neither
            # wins nor losses here (mirrors compare_versions._aggregate_pod).
            elif (
                game.winner_normalized is not None
                and game.winner_normalized != user_norm
                and game.end_turn is not None
            ):
                lost_turns.append(game.end_turn)
                if user_stats.eliminated and user_stats.eliminated_turn is not None:
                    cur = report.fastest_loss_turn
                    report.fastest_loss_turn = (
                        user_stats.eliminated_turn if cur is None
                        else min(cur, user_stats.eliminated_turn)
                    )

        # Per-opponent record: which decks beat the user, which ones fell.
        for d in parsed.deck_results:
            if _normalize(d.name) == user_norm:
                continue
            row = report.per_opponent_record.setdefault(
                d.normalized_name,
                {"games": 0, "wins_vs_user": 0},
            )
            row["games"] += parsed.games_completed
            row["wins_vs_user"] += d.wins

    # User wins are a subset of games_played; losses = decisive - wins.
    # games_played only counts ATTRIBUTED games — failed pods were excluded
    # in the loop above, so a crashed/timed-out pod can no longer inject
    # phantom losses here.
    decisive = report.games_played - report.draws
    report.user_losses = max(0, decisive - report.user_wins)
    if report.failed_pods:
        print(
            f"\nWARNING: {report.failed_pods}/{len(pods)} pod(s) failed and "
            f"were excluded ({report.excluded_games} unattributed game(s) "
            f"discarded). The record reflects {report.games_played} "
            f"attributed game(s) only.",
            flush=True,
        )
    report.avg_user_ending_life = round(
        sum(ending_lives) / len(ending_lives), 1
    ) if ending_lives else 0.0
    report.avg_user_damage_taken = round(
        sum(damages) / len(damages), 1
    ) if damages else 0.0
    report.avg_turns_when_won = round(
        sum(won_turns) / len(won_turns), 1
    ) if won_turns else 0.0
    report.avg_turns_when_lost = round(
        sum(lost_turns) / len(lost_turns), 1
    ) if lost_turns else 0.0

    # Persist.
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w-]+", "_", Path(user_deck).stem).strip("_") or "deck"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{stem}_B{bracket}_{ts}.json"
    out_path.write_text(report.to_json(), encoding="utf-8")
    print(f"\nWrote matchup report: {out_path}", flush=True)
    return report


def _format_summary(report: MatchupReport) -> str:
    lines = []
    lines.append(f"=== Matchup: {report.user_deck} (B{report.bracket}) ===")
    lines.append(f"Record: {report.user_wins}W / {report.user_losses}L / {report.draws}D — "
                 f"win rate {report.win_rate:.1%}")
    # Failure telemetry right under the record line — a record built on
    # fewer pods than requested reads very differently.
    if report.failed_pods or report.timed_out_pods:
        lines.append(
            f"!! {report.failed_pods} failed pod(s) EXCLUDED "
            f"({report.excluded_games} unattributed game(s) discarded), "
            f"{report.timed_out_pods} timed-out pod(s) truncated."
        )
        for f in report.pod_failures:
            lines.append(f"!!   pod {f['pod_index']}: {f['reason']}")
    lines.append(f"Avg ending life: {report.avg_user_ending_life}  (damage taken/game: {report.avg_user_damage_taken})")
    if report.avg_turns_when_won:
        lines.append(f"Avg turns when winning: {report.avg_turns_when_won}")
    if report.avg_turns_when_lost:
        lines.append(f"Avg turns when losing: {report.avg_turns_when_lost}")
    if report.fastest_loss_turn:
        lines.append(f"Fastest elimination: turn {report.fastest_loss_turn}")
    if report.unsupported_cards:
        lines.append(f"Unsupported cards encountered: {sorted(set(report.unsupported_cards))}")
    if report.per_opponent_record:
        lines.append("\nPer-opponent record:")
        for name, row in sorted(
            report.per_opponent_record.items(),
            key=lambda kv: kv[1]["wins_vs_user"],
            reverse=True,
        ):
            lines.append(f"  {name}: beat user {row['wins_vs_user']}/{row['games']} games")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="run_match")
    p.add_argument("--user", required=True, help="Filename of the [USER] deck under test.")
    p.add_argument("--bracket", type=int, required=True)
    p.add_argument("--games", type=int, default=3, help="Games per pod (default 3).")
    p.add_argument("--pods", type=int, default=2, help="Number of pods to play (default 2).")
    args = p.parse_args(argv)

    report = run_matchup(
        user_deck=args.user,
        bracket=args.bracket,
        games_per_pod=args.games,
        num_pods=args.pods,
    )
    summary = _format_summary(report)
    # Windows console default cp1252 chokes on emoji in opponent names. Re-encode
    # via the underlying buffer so the summary survives — JSON file already wrote
    # successfully via UTF-8.
    try:
        print("\n" + summary)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(("\n" + summary + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
