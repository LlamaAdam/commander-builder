# Orchestrator worklist

A work queue for the autonomous **commander-orchestrator**
(`C:\dev\commander-orchestrator`), which acts only on test failures: it runs the
target repo's pytest suite and, for each failure, routes a fix (tier-1 local
qwen / tier-2 Claude via `replace_file`) onto its own auto-fix branch, verifying
green before committing. It never weakens a test.

This branch (`orch/worklist`) is **reset onto the current `feature` tip**, so the
orchestrator builds on the latest code, and `tests/test_orch_worklist.py` holds
**intentionally-red** work items. `feature` + CI stay green; the red tests live
only here.

## Item selection rules (so the orchestrator can actually do the work)

1. **Single existing file.** The orchestrator's `replace_file` edits an existing
   file — it can't create new modules. Every item adds a function/behavior to a
   file that already exists, makeable green by one file edit.
2. **Test-pinned contract.** The failing test fully specifies inputs + outputs,
   so the implementation is unambiguous (no guessing).
3. **Not `forge_runner.py`** — that's the operator's active area.

## Point the orchestrator here

```powershell
cd C:\dev\commander-builder
git checkout orch/worklist          # (operator confirms / runs the orchestrator)
cd C:\dev\commander-orchestrator
.\.venv\Scripts\python -m orchestrator.cli fix --repo-dir C:\dev\commander-builder
```
Each fix lands on an orchestrator auto-fix branch off `orch/worklist`; review +
merge, or cherry-pick the source change back onto `feature`.

## Work items — LANDED 2026-05-27 (queue currently empty)

The four items below were implemented by the orchestrator, reviewed against
their pinned contracts, and merged to `feature` (`27e3423..0f94711`). Each
test was folded from `tests/test_orch_worklist.py` into its permanent
per-module home, so coverage now rides with the code. Full `--run-slow`
suite green (1618 passed). Seed the next batch here when there's new
orchestrator-suitable work.

| # | Test (now lives in) | File | What landed |
|---|---------------------|------|-------------|
| 1 | `tests/test_game_changers.py` | `game_changers.py` | (bug) don't persist the cache when the WotC scrape failed/returned empty, so the next call retries |
| 2 | `tests/test_margin_analysis.py` | `scripts/margin_analysis.py` | **FP-002**: `single_feature_ols(samples, feature)` — pure-stdlib OLS of margin~feature + leave-one-out RMSE (the analysis→predictor step) |
| 3 | `tests/test_bootstrap.py` | `bootstrap.py` | **FP-010**: `_pick_jre_asset(release, system, machine)` — choose the Temurin JRE archive for a platform (mirrors `_pick_forge_jar_asset`) |
| 4 | `tests/test_web_helpers.py` | `web/_helpers.py` | **FP-007**: `decks_containing_card(deck_dir, card_name)` — cross-deck library search; sorted deck IDs containing the card |

## Workflow

1. Operator confirms + runs the orchestrator against this branch.
2. Orchestrator implements each item on its auto-fix branches (red → green).
3. Operator confirms the work; then it's reviewed (does the implementation match
   the contract, no test weakening, no regressions) before merging the source
   changes back to `feature`.

## Deferred (not seeded — would be poor orchestrator tasks)

- `forge_runner.locate()` version-sort: real bug, but editing `forge_runner.py`
  collides with the operator's active work — hold until that settles.
- `fetch_average_deck` 40-copy basics: real, but the *correct* basic count is
  ambiguous (EDHREC gives no per-basic counts) — needs a human design call first.
- log_parser multi-word phase / EDHREC slug / scryfall TTL: investigated and
  found to be non-bugs or by-design (see session notes) — not seeded.
