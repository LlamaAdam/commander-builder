"""Top-level status command — `commander-status`.

One-glance health view of the project's on-disk state. Useful when picking
back up a project after a few days, or when troubleshooting "why isn't this
working" before reading any logs.

Reports four buckets:

  Decks       — counts per bracket on disk, [USER] vs filler split
  Pools       — curated pool JSONs in _pools/, freshness
  Matches     — recent run_match + compare_versions reports
  Knowledge   — iteration count, verdict distribution, latest deck per row

Output is plain text aimed at terminal scanning, not parsing. For
machine-readable output use `--json`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .forge_runner import VENDOR_FORGE
from .knowledge_log import DEFAULT_DB_PATH, recent_iterations, stats_summary

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
POOL_DIR = DECK_DIR / "_pools"
MATCH_DIR = DECK_DIR / "_matches"
COMPARE_DIR = DECK_DIR / "_compare"


@dataclass
class StatusReport:
    timestamp: str
    decks_by_bracket: dict[int, dict[str, int]] = field(default_factory=dict)
    pools_on_disk: list[dict] = field(default_factory=list)
    recent_matches: list[dict] = field(default_factory=list)
    recent_compares: list[dict] = field(default_factory=list)
    knowledge_log: dict = field(default_factory=dict)
    forge_dir_present: bool = False
    deck_dir_present: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _count_decks(deck_dir: Path) -> dict[int, dict[str, int]]:
    """Count decks at each bracket with [USER] vs filler split. Buckets are
    keyed on the bracket number (1-5). Decks without a `[B<n>]` suffix are
    silently ignored — they're not part of the bracket-tagged workflow."""
    out: dict[int, dict[str, int]] = {}
    if not deck_dir.exists():
        return out
    for path in deck_dir.glob("*.dck"):
        name = path.name
        # Find " [B<n>].dck" suffix (n in 1-5 or '?' for unknown).
        bracket: Optional[int] = None
        for n in (1, 2, 3, 4, 5):
            if name.endswith(f" [B{n}].dck"):
                bracket = n
                break
        if bracket is None:
            continue
        bucket = out.setdefault(bracket, {"user": 0, "filler": 0, "total": 0})
        if name.startswith("[USER]"):
            bucket["user"] += 1
        else:
            bucket["filler"] += 1
        bucket["total"] += 1
    return out


def _list_pools(pool_dir: Path) -> list[dict]:
    """Per-pool JSON summary. Sorted by mtime descending."""
    if not pool_dir.exists():
        return []
    out: list[dict] = []
    # Skip _analysis.json files; they're paired with the pool JSON and would
    # double-count.
    for path in sorted(
        pool_dir.glob("B*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if path.name.endswith("_analysis.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "path": path.name,
            "bracket": data.get("bracket"),
            "pool_a_size": len(data.get("pool_a", [])),
            "pool_b_size": len(data.get("pool_b", [])),
            "rejected_count": len(data.get("rejected", [])),
            "modified": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        })
    return out


def _list_recent(report_dir: Path, limit: int = 5) -> list[dict]:
    """Most-recent N JSON reports in a directory, by mtime. Used for both
    _matches/ and _compare/. Returns abbreviated rows."""
    if not report_dir.exists():
        return []
    paths = sorted(
        report_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # Both MatchupReport and ComparisonReport carry these top-level
        # fields. Defensive .get() handles either schema.
        out.append({
            "path": path.name,
            "timestamp": data.get("timestamp"),
            "bracket": data.get("bracket"),
            "total_games": data.get("total_games") or data.get("games_played"),
            "winner": data.get("winner"),
            "win_rate": data.get("win_rate"),
        })
    return out


def collect_status(
    deck_dir: Path = DECK_DIR,
    pool_dir: Path = POOL_DIR,
    match_dir: Path = MATCH_DIR,
    compare_dir: Path = COMPARE_DIR,
    db_path: Path = DEFAULT_DB_PATH,
) -> StatusReport:
    """Gather all status signals into one StatusReport. Pure read — never
    writes anything (other than knowledge_log's idempotent schema-init via
    `stats_summary`)."""
    report = StatusReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        forge_dir_present=VENDOR_FORGE.exists(),
        deck_dir_present=deck_dir.exists(),
        decks_by_bracket=_count_decks(deck_dir),
        pools_on_disk=_list_pools(pool_dir),
        recent_matches=_list_recent(match_dir),
        recent_compares=_list_recent(compare_dir),
    )

    # knowledge_log: latest 5 + aggregate stats.
    try:
        kl_stats = stats_summary(db_path=db_path)
        recent = recent_iterations(limit=5, db_path=db_path)
        report.knowledge_log = {
            "stats": kl_stats,
            "recent": [
                {
                    "id": it.id,
                    "deck_id": it.deck_id,
                    "deck_name": it.deck_name,
                    "bracket": it.bracket,
                    "verdict": it.verdict,
                    "margin": it.margin,
                    "created_at": it.created_at,
                }
                for it in recent
            ],
        }
    except Exception as exc:
        report.knowledge_log = {"error": f"{type(exc).__name__}: {exc}"}

    return report


def format_text(report: StatusReport) -> str:
    """Render a StatusReport as terminal-friendly plain text."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f" Commander Builder — status as of {report.timestamp}")
    lines.append("=" * 60)
    lines.append("")

    # Forge availability.
    forge_marker = "yes" if report.forge_dir_present else "no (live sims will fail)"
    lines.append(f"Forge install present: {forge_marker}")
    lines.append("")

    # Decks.
    lines.append("Decks on disk")
    lines.append("-" * 60)
    if report.decks_by_bracket:
        for bracket in sorted(report.decks_by_bracket.keys()):
            row = report.decks_by_bracket[bracket]
            lines.append(
                f"  B{bracket}: {row['total']:>4} total  "
                f"({row['user']} [USER] + {row['filler']} filler)"
            )
    else:
        lines.append("  (no decks found — run `commander-import --harvest <bracket>`)")
    lines.append("")

    # Pools.
    lines.append(f"Curated pools ({len(report.pools_on_disk)})")
    lines.append("-" * 60)
    if report.pools_on_disk:
        for p in report.pools_on_disk:
            lines.append(
                f"  {p['path']:>20s}  pool_a={p['pool_a_size']}  pool_b={p['pool_b_size']}  "
                f"rejected={p['rejected_count']}"
            )
    else:
        lines.append("  (no curated pools — run `commander-curate --bracket <n>`)")
    lines.append("")

    # Recent matches + compares.
    lines.append(f"Recent run_match reports ({len(report.recent_matches)})")
    lines.append("-" * 60)
    if report.recent_matches:
        for m in report.recent_matches:
            wr = f"{m['win_rate']:.1%}" if m.get("win_rate") is not None else "?"
            lines.append(f"  {m['path']:<60s} B{m.get('bracket', '?')} win_rate={wr}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Recent comparison reports ({len(report.recent_compares)})")
    lines.append("-" * 60)
    if report.recent_compares:
        for c in report.recent_compares:
            lines.append(
                f"  {c['path']:<60s} B{c.get('bracket', '?')} winner={c.get('winner', '?')}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Knowledge log.
    lines.append("Knowledge log")
    lines.append("-" * 60)
    kl = report.knowledge_log
    if "error" in kl:
        lines.append(f"  ERROR: {kl['error']}")
    else:
        s = kl.get("stats", {})
        lines.append(
            f"  total={s.get('total', 0)}  unique_decks={s.get('unique_decks', 0)}  "
            f"kept={s.get('kept', 0)}  reverted={s.get('reverted', 0)}  "
            f"neutral={s.get('neutral', 0)}  pending={s.get('pending', 0)}"
        )
        recent = kl.get("recent", [])
        if recent:
            lines.append("  Recent iterations:")
            for it in recent:
                margin_s = f"{it['margin']:+d}" if it.get("margin") is not None else "?"
                lines.append(
                    f"    #{it['id']:<4d} B{it['bracket']}  "
                    f"verdict={it['verdict']:<8s}  margin={margin_s:<5s}  "
                    f"deck={it['deck_name']}"
                )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="commander-status",
                                description="One-glance project health.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of text.")
    args = p.parse_args(argv)

    report = collect_status()
    if args.json:
        try:
            print(report.to_json())
        except UnicodeEncodeError:
            sys.stdout.buffer.write((report.to_json() + "\n").encode("utf-8", errors="replace"))
        return 0

    text = format_text(report)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
