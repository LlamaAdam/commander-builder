"""Phase 2 orchestrator: propose → simulate → analyze → commit/revert.

Glues the existing pieces together in one repeatable cycle:

    1. snapshot_deck(deck, version=v_n)              — freeze baseline
    2. (LLM proposer)                                — produce audit_manifest
       — Today: Moxfield audit prompt in a Claude session.
       — Phase 2 v1: programmatic Claude call replacing the manual paste.
    3. moxfield_import.import_deck(...)              — re-pull post-audit deck
    4. snapshot_deck(deck, version=v_n+1)            — freeze post-audit
    5. compare_versions.compare(v_n, v_n+1, ...)     — empirical sim
    6. analyst.analyze(...)                          — verdict
    7. knowledge_log.record_iteration(...)           — persist row
    8. If verdict == "reverted":
        — restore v_n on Moxfield (manual paste via moxfield_push.prepare_push)
        — log the revert
       Else: continue to next iteration.

This module wires it together. The LLM proposer step (2) is currently a
hand-off to the user — they paste the audit prompt and produce a manifest
JSON we ingest. Replace `_user_provided_manifest` with a programmatic Claude
call when ready.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import re

from .analyst import AnalystConfig, AnalystInput, Verdict, analyze
from .compare_versions import ComparisonReport, compare
from .forge_runner import VENDOR_FORGE
# ``db_path=None`` defaults below defer to knowledge_log's call-time
# resolver — a ``= DEFAULT_DB_PATH`` def-time default would freeze the
# production path and bypass the test suite's isolation patch.
from .knowledge_log import (
    Iteration,
    record_iteration,
)
from .proposer import ProposerConfig, ProposerInput, propose
from .snapshot_deck import snapshot

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"

# `Moxfield=<publicId>` lines in the .dck metadata block let us recover the
# durable deck identity even after the file is renamed (e.g. user changes
# the deck name on Moxfield). Regex pinned at module level so it's compiled
# once.
_MOXFIELD_ID = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)


def resolve_deck_id(deck_path: Path, fallback: Optional[str] = None) -> str:
    """Read the Moxfield publicId from the .dck metadata block.

    Falls back to the filename stem if the deck wasn't imported via
    `moxfield_import` (older deck, or hand-built locally) so the iteration
    loop still works on legacy decks. Returns `fallback` only if no
    `Moxfield=` line exists AND no fallback is supplied; in that case raises
    `ValueError` so callers can't silently drop into filename-as-id mode by
    accident.
    """
    if not deck_path.exists():
        if fallback is not None:
            return fallback
        raise ValueError(f"deck not found and no fallback: {deck_path}")
    text = deck_path.read_text(encoding="utf-8")
    m = _MOXFIELD_ID.search(text)
    if m:
        return m.group(1).strip()
    if fallback is not None:
        return fallback
    # Last resort: filename stem (without .dck and bracket suffix). Stable
    # within a session but breaks on rename.
    return deck_path.stem


@dataclass
class IterationResult:
    iteration_id: int
    verdict: Verdict
    comparison: ComparisonReport
    next_action: str         # "continue" | "revert" | "stop"


def propose_then_iterate(
    deck_filename: str,
    new_deck_filename: str,
    bracket: int,
    *,
    parent_iteration_id: Optional[int] = None,
    games_per_pod: int = 10,
    filler_pairs: int = 2,
    db_path: Optional[Path] = None,
    analyst_config: Optional[AnalystConfig] = None,
    proposer_config: Optional[ProposerConfig] = None,
) -> "IterationResult":
    """Convenience wrapper: pull the manifest via `propose()`, then run one
    iteration. Closes the manual paste loop — when `proposer_config.use_claude`
    is True (and the SDK is wired), the audit happens programmatically.
    Otherwise falls back to reading a manifest file."""
    proposer_config = proposer_config or ProposerConfig()
    new_path = DECK_DIR / new_deck_filename
    proposer_input = ProposerInput(
        deck_path=new_path,
        bracket=bracket,
        deck_id=resolve_deck_id(DECK_DIR / deck_filename, fallback=deck_filename),
    )
    manifest = propose(proposer_input, proposer_config).to_dict()
    return run_one_iteration(
        deck_filename=deck_filename,
        new_deck_filename=new_deck_filename,
        bracket=bracket,
        audit_manifest=manifest,
        parent_iteration_id=parent_iteration_id,
        games_per_pod=games_per_pod,
        filler_pairs=filler_pairs,
        db_path=db_path,
        analyst_config=analyst_config,
    )


def run_one_iteration(
    deck_filename: str,
    bracket: int,
    audit_manifest: dict,
    new_deck_filename: str,
    *,
    parent_iteration_id: Optional[int] = None,
    games_per_pod: int = 10,
    filler_pairs: int = 2,
    db_path: Optional[Path] = None,
    analyst_config: Optional[AnalystConfig] = None,
) -> IterationResult:
    """Run one full propose→simulate→analyze cycle for a deck.

    Caller is responsible for:
      - snapshotting `deck_filename` to a versioned path BEFORE running the
        Moxfield audit (so the v1 baseline is preserved)
      - re-importing the post-audit deck (overwrites `deck_filename`)
      - snapshotting again to `new_deck_filename` (the v2 path)

    This function takes those two snapshot paths plus the audit manifest, runs
    `compare()`, asks the analyst, persists to the knowledge log, and returns
    a recommendation."""
    old_path = DECK_DIR / deck_filename
    new_path = DECK_DIR / new_deck_filename

    # Step 5: head-to-head sim.
    cmp_report = compare(
        old_deck=deck_filename,
        new_deck=new_deck_filename,
        bracket=bracket,
        games_per_pod=games_per_pod,
        filler_pairs=filler_pairs,
    )

    # Step 6: verdict.
    #
    # Guard ("no silent failures"): if every pod failed — JVM crash,
    # timeout with nothing salvageable — compare() returns total_games=0.
    # The old code divided by max(1, total - draws) and recorded
    # win_rate_old=0.0 / win_rate_new=0.0, a FABRICATED "empirical
    # neutral" that poisoned the knowledge log as if 0-0 had actually
    # been observed at the table. Refuse to render an empirical verdict
    # on zero attributed games: record the row as 'pending' (the same
    # label _proposer_sim._verdict_from_ab uses for skipped/failed sims)
    # with NULL win rates, so the iteration can be re-run later and no
    # analyst/LLM wastes a call on an empty sim.
    decisive = cmp_report.total_games - cmp_report.draws
    if cmp_report.total_games == 0:
        reason = (
            f"sim produced no attributed games "
            f"({cmp_report.failed_pods}/{cmp_report.pods_planned or len(cmp_report.pods)} "
            f"pod(s) failed, {cmp_report.excluded_games} unattributed game(s) "
            f"discarded) — no empirical verdict recorded"
        )
        print(f"WARNING: {reason}", flush=True)
        verdict = Verdict(
            label="pending",
            confidence=0.0,
            reasoning=reason,
            lessons=[],
            source="harness",
        )
        win_rate_old = None
        win_rate_new = None
        margin = None
    else:
        verdict = analyze(
            AnalystInput(
                deck_name=deck_filename,
                bracket=bracket,
                audit_manifest=audit_manifest,
                sim_report=cmp_report.to_dict(),
            ),
            config=analyst_config,
        )
        # Draws are excluded from the denominator. If every attributed
        # game drew there is no decisive sample — record NULL win rates
        # rather than a fake 0.0/0.0 (the old max(1, ...) clamp did
        # exactly that).
        win_rate_old = (
            round(cmp_report.old_stats.wins / decisive, 3) if decisive > 0 else None
        )
        win_rate_new = (
            round(cmp_report.new_stats.wins / decisive, 3) if decisive > 0 else None
        )
        margin = cmp_report.new_stats.wins - cmp_report.old_stats.wins

    # Step 7: persist.
    snapshot_text = new_path.read_text(encoding="utf-8") if new_path.exists() else None
    # Use the Moxfield publicId from the .dck metadata as the durable deck_id.
    # Falls back to the filename if the deck pre-dates the Moxfield= metadata
    # patch (legacy import). Either side of the v1/v2 pair carries the same
    # publicId, so old_path is the canonical source.
    deck_id = resolve_deck_id(old_path, fallback=deck_filename)
    iteration_id = record_iteration(
        Iteration(
            deck_id=deck_id,
            deck_name=deck_filename,
            bracket=bracket,
            parent_id=parent_iteration_id,
            audit_version=audit_manifest.get("audit_version", "v3"),
            audit_manifest=audit_manifest,
            sim_report=cmp_report.to_dict(),
            verdict=verdict.label,
            verdict_notes=verdict.reasoning,
            win_rate_old=win_rate_old,
            win_rate_new=win_rate_new,
            margin=margin,
            deck_snapshot=snapshot_text,
        ),
        db_path=db_path,
    )

    next_action = {
        "kept": "continue",
        "reverted": "revert",
        "neutral": "stop",  # User decides; loop pauses by default.
        # Sim failed outright (zero attributed games) — nothing empirical
        # happened, so the only sane move is to stop and let the user
        # investigate/re-run. Continuing would iterate on unverified data.
        "pending": "stop",
    }[verdict.label]

    return IterationResult(
        iteration_id=iteration_id,
        verdict=verdict,
        comparison=cmp_report,
        next_action=next_action,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Single-iteration CLI.

    Two modes:
      --manifest <path>       (manual)  Pre-built manifest from the audit prompt.
      --auto-propose          (claude)  Programmatic Claude proposer call.
                                        Requires ANTHROPIC_API_KEY + `pip install anthropic`.
    """
    p = argparse.ArgumentParser(prog="iteration_loop")
    p.add_argument("--old", required=True, help="v_n filename (pre-audit snapshot).")
    p.add_argument("--new", required=True, help="v_(n+1) filename (post-audit snapshot).")
    p.add_argument("--bracket", type=int, required=True)
    p.add_argument("--manifest", help="Path to audit_manifest.json (manual mode).")
    p.add_argument("--auto-propose", action="store_true",
                   help="Invoke proposer.propose() with use_claude=True instead of "
                        "reading a manifest file. Falls back to manual if Claude unwired.")
    p.add_argument("--games", type=int, default=10)
    p.add_argument("--filler-pairs", type=int, default=2)
    p.add_argument("--parent-id", type=int, default=None,
                   help="ID of the previous iteration of this deck, if any.")
    args = p.parse_args(argv)

    if args.auto_propose:
        result = propose_then_iterate(
            deck_filename=args.old,
            new_deck_filename=args.new,
            bracket=args.bracket,
            parent_iteration_id=args.parent_id,
            games_per_pod=args.games,
            filler_pairs=args.filler_pairs,
            proposer_config=ProposerConfig(use_claude=True),
        )
    else:
        if not args.manifest:
            p.error("Either --manifest <path> or --auto-propose is required.")
        manifest_text = Path(args.manifest).read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        result = run_one_iteration(
            deck_filename=args.old,
            new_deck_filename=args.new,
            bracket=args.bracket,
            audit_manifest=manifest,
            parent_iteration_id=args.parent_id,
            games_per_pod=args.games,
            filler_pairs=args.filler_pairs,
        )

    print(f"\nIteration #{result.iteration_id} — verdict: {result.verdict.label} "
          f"(confidence {result.verdict.confidence:.2f}, source {result.verdict.source})")
    print(f"  Reasoning: {result.verdict.reasoning}")
    if result.verdict.lessons:
        print(f"  Lessons:")
        for lesson in result.verdict.lessons:
            print(f"    - {lesson}")
    print(f"  Recommended action: {result.next_action.upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
