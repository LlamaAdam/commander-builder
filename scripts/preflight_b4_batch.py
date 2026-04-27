"""One-shot preflight runner for the B4 deck batch.

Stays in-process so JVM startup (~3-4s) only fires once per sim, and so we get
a single consolidated report at the end. Writes the full results to
`b4_preflight_results.json` for post-mortem analysis.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from commander_builder.forge_runner import ForgeRunner  # noqa: E402
from commander_builder.pool_curator import preflight_candidate  # noqa: E402

TARGETS = [
    "[USER] Green Gobby [B4].dck",
    "[USER] Black Mage Blitz [B4].dck",
    "[USER] Wyrm Sovereign [B4].dck",
    "[USER] Goblin [B4].dck",
    "[USER] Eldrazi [B4].dck",
    "[USER] Dog Moves [B4].dck",
]
FILLERS = [
    "Abzan Frog [B4].dck",
    "Ashling [B4].dck",
    "Atraxa Infect_Proliferate [B4].dck",
]

runner = ForgeRunner.locate()
results: list[dict] = []
for i, target in enumerate(TARGETS, 1):
    print(f"\n--- [{i}/{len(TARGETS)}] {target} ---", flush=True)
    pre = preflight_candidate(runner, target, FILLERS)
    summary = {
        "target": target,
        "crashed": pre.crashed,
        "crash_reason": pre.crash_reason,
        "unsupported": pre.unsupported,
        "unsupported_cards": pre.parsed.unsupported_cards[:10],
        "games_completed": pre.parsed.games_completed,
        "avg_game_sec": round(pre.parsed.avg_game_sec, 1),
        "confirm_action_per_game": round(pre.parsed.confirm_action_per_game, 2),
        "confirm_action_cards": pre.parsed.confirm_action_cards[:10],
        "deck_results": [(d.name, d.wins) for d in pre.parsed.deck_results],
    }
    print(json.dumps(summary, indent=2), flush=True)
    results.append(summary)

out = REPO / "b4_preflight_results.json"
out.write_text(json.dumps(results, indent=2), encoding="utf-8")
print(f"\nWrote {out}", flush=True)

passed = [r for r in results if not r["crashed"] and r["unsupported"] == 0]
failed = [r for r in results if r["crashed"] or r["unsupported"] > 0]
print(f"\n=== B4 batch summary: {len(passed)}/{len(results)} pass ===")
for r in failed:
    reason = r["crash_reason"] or f"unsupported={r['unsupported']}"
    print(f"  FAIL {r['target']}: {reason}")
