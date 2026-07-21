"""End-to-end smoke test of compare_versions.

Uses two distinct user decks (Hakbal as 'old', Hash as 'new') in place of a
real v1/v2 iteration pair. The card diff and 'winner' verdict are not
meaningful — they're different decks, not versions — but every pipeline stage
runs under real Forge load. Successful exit + a well-formed ComparisonReport
JSON is the pass criterion.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from commander_builder.compare_versions import compare, _format_summary  # noqa: E402

report = compare(
    old_deck="[USER] Hakbal of the Surging Soul [B3].dck",
    new_deck="[USER] Hash [B3].dck",
    bracket=3,
    games_per_pod=10,
    filler_pairs=2,
    mode="pod",
)
summary = _format_summary(report)
try:
    print("\n" + summary, flush=True)
except UnicodeEncodeError:
    sys.stdout.buffer.write(("\n" + summary + "\n").encode("utf-8", errors="replace"))

# Pass criteria: structured exit signal for the harness.
print(f"\nSMOKE: total_games={report.total_games}, winner={report.winner}, "
      f"margin={report.margin}, draws={report.draws}", flush=True)
print(f"SMOKE: card_diff_added={len(report.card_diff.get('added', []))}, "
      f"removed={len(report.card_diff.get('removed', []))}", flush=True)
print(f"SMOKE: pods_run={len(report.pods)}", flush=True)
print("SMOKE: PASS" if report.total_games > 0 else "SMOKE: FAIL (no games)", flush=True)
