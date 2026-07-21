"""Render a deck's full iteration lineage as a human-readable Markdown report.

Once iterations accumulate, scrolling through the SQLite knowledge_log to see
"what happened to deck X over time" becomes a chore. This module reads the
log, rebuilds the v1 → v2 → ... → vN chain, and emits a Markdown doc with
per-iteration card-diff tables and verdict reasoning.

Public API:

    from commander_builder.report import render_deck_history

    md = render_deck_history(deck_id="abc-XYZ")
    # → Markdown string ready to write to disk or paste anywhere

CLI:

    commander-history --deck-id abc-XYZ
    commander-history --deck-id abc-XYZ --output deck_history.md

Phase 2 deliverable from PROJECT.md (the long-promised `report.py`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# ``db_path=None`` defaults below defer to knowledge_log's call-time
# resolver — a ``= DEFAULT_DB_PATH`` def-time default would freeze the
# production path and bypass the test suite's isolation patch.
from .knowledge_log import (
    Iteration,
    iterations_for_deck,
    recent_iterations,
)


def _format_card_diff(manifest: Optional[dict]) -> str:
    """Two-column markdown showing added vs removed cards. If both are empty
    (e.g. a revert iteration), returns "_No card changes._"."""
    if not manifest:
        return "_No manifest recorded._"
    added = manifest.get("added", []) or []
    removed = manifest.get("removed", []) or []
    if not added and not removed:
        return "_No card changes._"
    rows = max(len(added), len(removed))
    lines = ["| Added | Removed |", "|-------|---------|"]
    for i in range(rows):
        a = added[i] if i < len(added) else ""
        r = removed[i] if i < len(removed) else ""
        lines.append(f"| {a} | {r} |")
    return "\n".join(lines)


def _format_sim_summary(sim_report: Optional[dict]) -> str:
    """One-line numeric summary of a sim report. Tolerates either
    ComparisonReport or MatchupReport shape."""
    if not sim_report:
        return "_No sim report._"
    total = sim_report.get("total_games") or sim_report.get("games_played", 0)
    draws = sim_report.get("draws", 0)
    if "old_stats" in sim_report and "new_stats" in sim_report:
        # ComparisonReport
        old_w = sim_report["old_stats"].get("wins", 0)
        new_w = sim_report["new_stats"].get("wins", 0)
        winner = sim_report.get("winner", "?")
        margin = sim_report.get("margin", 0)
        return (
            f"**Head-to-head**: OLD {old_w} – {new_w} NEW over {total} games "
            f"({draws} draws). Winner: **{winner}** (margin {margin})."
        )
    if "user_wins" in sim_report:
        # MatchupReport
        wins = sim_report["user_wins"]
        losses = sim_report.get("user_losses", 0)
        wr = sim_report.get("win_rate", 0)
        return f"**vs pool**: {wins}W / {losses}L over {total} games ({draws} draws). Win rate: {wr:.1%}."
    return f"_Sim ran for {total} games._"


def _verdict_badge(verdict: str) -> str:
    """Markdown badge for the verdict label. Uses Unicode arrows so the
    output works in any markdown viewer."""
    badges = {
        "kept": "↑ KEPT",
        "reverted": "↓ REVERTED",
        "neutral": "→ NEUTRAL",
        "pending": "… PENDING",
    }
    return badges.get(verdict, f"? {verdict.upper()}")


def render_iteration(it: Iteration, position: int, total: int) -> str:
    """Render one iteration as a Markdown section."""
    lines: list[str] = []
    header = f"## Iteration {position}/{total} — #{it.id}"
    if it.audit_version:
        header += f" (audit {it.audit_version})"
    lines.append(header)

    meta_bits: list[str] = []
    if it.created_at:
        meta_bits.append(f"`{it.created_at}`")
    if it.bracket:
        meta_bits.append(f"B{it.bracket}")
    meta_bits.append(_verdict_badge(it.verdict))
    if it.win_rate_old is not None and it.win_rate_new is not None:
        meta_bits.append(
            f"win rate: {it.win_rate_old:.0%} → {it.win_rate_new:.0%}"
        )
    if it.margin is not None:
        sign = "+" if it.margin > 0 else ""
        meta_bits.append(f"margin: {sign}{it.margin}")
    lines.append(" • ".join(meta_bits))
    lines.append("")

    # Rationale.
    # `or ""` (not a .get default): a manifest with an explicit JSON null
    # ({"rationale": null}) makes .get return None despite the default, and
    # None.strip() would kill the whole history render for one bad row.
    rationale = ((it.audit_manifest or {}).get("rationale") or "").strip()
    if rationale:
        lines.append(f"**Rationale**: {rationale}")
        lines.append("")

    # Verdict reasoning.
    if it.verdict_notes:
        lines.append(f"**Analyst**: {it.verdict_notes}")
        lines.append("")

    # Sim numbers.
    lines.append(_format_sim_summary(it.sim_report))
    lines.append("")

    # Card diff.
    lines.append("### Card changes")
    lines.append(_format_card_diff(it.audit_manifest))
    lines.append("")

    # Lineage pointer.
    if it.parent_id:
        lines.append(f"_Parent iteration: #{it.parent_id}_")
    lines.append("")
    return "\n".join(lines)


def render_deck_history(
    deck_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """Build the full Markdown report for one deck's iteration chain."""
    history = iterations_for_deck(deck_id, db_path=db_path)
    if not history:
        return f"# No iterations found for deck `{deck_id}`\n"

    lines: list[str] = []
    first = history[0]
    lines.append(f"# {first.deck_name}")
    lines.append("")
    lines.append(f"**Deck ID**: `{deck_id}`")
    lines.append(f"**Bracket**: B{first.bracket}")
    lines.append(f"**Iterations**: {len(history)}")

    # Verdict tally.
    from collections import Counter
    verdict_counts = Counter(it.verdict for it in history)
    parts = [f"{v}: {c}" for v, c in sorted(verdict_counts.items())]
    lines.append(f"**Verdict tally**: {', '.join(parts)}")
    lines.append("")

    # Win-rate trajectory.
    measured = [it for it in history if it.win_rate_new is not None]
    if measured:
        first_wr = measured[0].win_rate_new
        last_wr = measured[-1].win_rate_new
        delta = (last_wr or 0) - (first_wr or 0)
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"**Win-rate trajectory**: {first_wr:.0%} → {last_wr:.0%} "
            f"({sign}{delta:.0%} over {len(measured)} measured iterations)"
        )
        lines.append("")

    lines.append("---")
    lines.append("")

    for i, it in enumerate(history, 1):
        lines.append(render_iteration(it, position=i, total=len(history)))
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def render_recent_iterations_summary(
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> str:
    """Cross-deck Markdown summary of recent iterations. Useful when you want
    a "what's been happening across the project" view rather than per-deck
    history."""
    rows = recent_iterations(limit=limit, db_path=db_path)
    if not rows:
        return "# No iterations recorded yet.\n"
    lines = [
        f"# Recent iterations (last {len(rows)})",
        "",
        "| # | Deck | B | Verdict | Margin | Created |",
        "|---|------|---|---------|--------|---------|",
    ]
    for it in rows:
        m = "?" if it.margin is None else (f"+{it.margin}" if it.margin > 0 else str(it.margin))
        lines.append(
            f"| #{it.id} | {it.deck_name} | B{it.bracket} | "
            f"{_verdict_badge(it.verdict)} | {m} | {it.created_at or ''} |"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-history",
        description="Render a deck's iteration lineage as Markdown.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--deck-id", help="Render full history for this deck.")
    g.add_argument("--recent", action="store_true",
                   help="Render a cross-deck summary of recent iterations.")
    p.add_argument("--limit", type=int, default=20,
                   help="Max iterations for --recent (default 20).")
    p.add_argument("--output", help="Write to this path instead of stdout.")
    args = p.parse_args(argv)

    if args.deck_id:
        text = render_deck_history(args.deck_id)
    else:
        text = render_recent_iterations_summary(limit=args.limit)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
        return 0

    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
