"""Seed a demo knowledge_log.sqlite for the FP-006 dashboard.

The eventual web UI consumes both ``deck_dashboard.build_dashboard()``
output (single-deck snapshot) and the iteration history from
``knowledge_log`` (the version timeline + win-rate curve at the bottom
of the mockup). To exercise the latter without running real Forge
matches we synthesize a small but realistic history for one deck.

Usage:
    python -m commander_builder.scripts.seed_demo_knowledge_log \\
        --db demo.sqlite --deck-id omnath-locus-of-creation

Or as a script:
    python scripts/seed_demo_knowledge_log.py --db demo.sqlite

The seeded data is *fictional*. It models what a 4-iteration tuning
arc looks like (one revert, one neutral, two kept improvements) so the
UI can render the version-history strip and the verdict pills.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a script when src/ isn't on sys.path.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commander_builder.knowledge_log import (  # noqa: E402
    Iteration,
    init_db,
    record_iteration,
)


# Four-iteration arc for "Omnath, Locus of Creation" — matches the UI mockup.
_DEMO_ITERATIONS = [
    {
        "label": "v1 — initial brew",
        "audit_version": "v1",
        "audit_manifest": {
            "added": [],
            "removed": [],
            "rationale": "Baseline — initial Moxfield import.",
        },
        "win_rate_old": None,
        "win_rate_new": 0.41,
        "margin": None,
        "verdict": "pending",
        "verdict_notes": "Baseline. No comparison possible yet.",
    },
    {
        "label": "v2 — landfall package",
        "audit_version": "v3",
        "audit_manifest": {
            "added": ["Lotus Cobra", "Tireless Tracker", "Field of the Dead"],
            "removed": ["Cultivate", "Kodama's Reach", "Wood Elves"],
            "rationale": (
                "Trade slow ramp for landfall payoffs that synergize with the "
                "commander's land-drop trigger."
            ),
        },
        "win_rate_old": 0.41,
        "win_rate_new": 0.52,
        "margin": 11,
        "verdict": "kept",
        "verdict_notes": "+11pp absolute winrate vs v1. Keep.",
    },
    {
        "label": "v3 — counterspell experiment",
        "audit_version": "v3",
        "audit_manifest": {
            "added": ["Mana Drain", "Force of Will", "Counterspell"],
            "removed": ["Beast Within", "Krosan Grip", "Nature's Claim"],
            "rationale": (
                "Try blue-leaning interaction. Hypothesis: hard counters beat "
                "instant-speed removal in multiplayer."
            ),
        },
        "win_rate_old": 0.52,
        "win_rate_new": 0.46,
        "margin": -6,
        "verdict": "reverted",
        "verdict_notes": (
            "Counterspells whiff in 3+ player games — too easy to play around. "
            "Reverted; v2 still champion."
        ),
    },
    {
        "label": "v4 — tutor smoothing",
        "audit_version": "v3",
        "audit_manifest": {
            "added": ["Worldly Tutor", "Sylvan Tutor"],
            "removed": ["Farseek", "Rampant Growth"],
            "rationale": "Replace redundant 2-mana ramp with green tutors.",
        },
        "win_rate_old": 0.52,
        "win_rate_new": 0.54,
        "margin": 2,
        "verdict": "neutral",
        "verdict_notes": "+2pp — within noise. Keep but flag for re-test.",
    },
]


def seed_demo(db_path: Path, deck_id: str = "omnath-locus-of-creation") -> int:
    """Write the demo arc to ``db_path``. Returns the id of the final row."""
    init_db(db_path)
    parent_id: int | None = None
    base_time = datetime.now(timezone.utc) - timedelta(days=14)
    last_id = -1

    for i, spec in enumerate(_DEMO_ITERATIONS):
        created_at = (base_time + timedelta(days=i * 3)).isoformat()
        snapshot = (
            "[metadata]\n"
            f"Name=Omnath {spec['label']}\n\n"
            "[Commander]\n"
            "1 Omnath, Locus of Creation\n\n"
            "[Main]\n"
            + "1 Forest\n" * 30
            + "1 Cultivate\n"
            + "1 Lotus Cobra\n"
        )
        it = Iteration(
            deck_id=deck_id,
            deck_name=f"Omnath {spec['label']}",
            bracket=3,
            parent_id=parent_id,
            audit_version=spec["audit_version"],
            audit_manifest=spec["audit_manifest"],
            sim_report=None,
            verdict=spec["verdict"],
            verdict_notes=spec["verdict_notes"],
            win_rate_old=spec["win_rate_old"],
            win_rate_new=spec["win_rate_new"],
            margin=spec["margin"],
            created_at=created_at,
            deck_snapshot=snapshot,
        )
        last_id = record_iteration(it, db_path=db_path)
        parent_id = last_id

    return last_id


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db", type=Path, default=Path("demo_knowledge_log.sqlite"),
        help="Output SQLite path (default: ./demo_knowledge_log.sqlite)",
    )
    p.add_argument(
        "--deck-id", default="omnath-locus-of-creation",
        help="Deck id used as the iteration parent key.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Delete the file first if it already exists.",
    )
    args = p.parse_args(argv)

    if args.db.exists() and args.force:
        args.db.unlink()

    last_id = seed_demo(args.db, deck_id=args.deck_id)
    print(f"Seeded {len(_DEMO_ITERATIONS)} iterations -> {args.db} (last id {last_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
