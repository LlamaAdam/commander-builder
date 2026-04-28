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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .forge_runner import ForgeRunner, VENDOR_FORGE
from .game_analyzer import analyze
from .log_parser import _normalize, parse
from .run_match import _fallback_opponents, _load_pool

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
COMPARE_OUT_DIR = DECK_DIR / "_compare"

# Default filler pairs per comparison run. Two pairs averages out single-pair
# bias (an unlucky filler choice that happens to counter one version).
DEFAULT_FILLER_PAIRS = 2
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
    """Return the lines under [Main] in a Forge .dck. Used for card-level diff."""
    if not deck_path.exists():
        return []
    text = deck_path.read_text(encoding="utf-8")
    out: list[str] = []
    in_main = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() == "[main]":
            in_main = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = False
            continue
        if in_main:
            out.append(stripped)
    return out


def _main_lines_from_text(text: str) -> list[str]:
    """Parse [Main] lines out of a .dck blob string. Same logic as
    `_read_main_section` but takes the raw text instead of a path —
    useful for diffing snapshots stored in knowledge_log without
    materializing temp files."""
    if not text:
        return []
    out: list[str] = []
    in_main = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() == "[main]":
            in_main = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = False
            continue
        if in_main:
            out.append(stripped)
    return out


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


def compare(
    old_deck: str,
    new_deck: str,
    bracket: int,
    games_per_pod: int = DEFAULT_GAMES_PER_POD,
    filler_pairs: int = DEFAULT_FILLER_PAIRS,
    mode: str = "pod",
    runner: Optional[ForgeRunner] = None,
    out_dir: Path = COMPARE_OUT_DIR,
) -> ComparisonReport:
    """Run the head-to-head comparison and persist the report."""
    if mode not in {"pod", "1v1"}:
        raise ValueError(f"mode must be 'pod' or '1v1', got {mode!r}")
    runner = runner or ForgeRunner.locate()

    old_path = DECK_DIR / old_deck
    new_path = DECK_DIR / new_deck
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

    for i, pod in enumerate(pods):
        print(f"\n--- Pod {i + 1}/{len(pods)}: {pod} ---", flush=True)
        if mode == "1v1":
            result = runner.run(pod, num_games=games_per_pod, game_format="constructed")
        else:
            result = runner.run(pod, num_games=games_per_pod)
        parsed = parse(result.stdout)
        ma = analyze(result.stdout)

        report.pods.append({
            "pod_index": i + 1,
            "pod": pod,
            "duration_sec": round(result.duration_sec, 1),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "match": ma.to_dict(),
        })
        report.total_games += parsed.games_completed
        report.draws += parsed.draws

        _aggregate_pod(
            report.old_stats, report.new_stats,
            parsed, ma, old_norm, new_norm,
            won_turns, lost_turns, ending_lives, damages,
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
