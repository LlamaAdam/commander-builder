# FP-002 data-gen — Machine 2 briefing (2026-05-24)

Goal: generate **trustworthy** post-seat-fix rows for the FP-002 margin-regression
reframe (target ~200; FP-013 needs far more). Machine 1 is running a 24h campaign;
this is the matching config + coordination for Machine 2.

## ⚠️ CRITICAL — do this FIRST or you waste 24h
**Machine 2 MUST be on commander-builder code containing the seat-attribution fix**
(commit `e8777b6` "fix(forge): attribute A/B sim wins by seat, not deck name").
Before the fix, A/B wins were credited by deck *name* — and the curated/detuned
decks share a `Name=`, so wins were mis-credited or zeroed. **Every row generated
on pre-fix code is an artifact** (this is why ~290 of the existing 306 rows are junk).

Verify:
```bash
cd <commander-builder>
git merge-base --is-ancestor e8777b6 HEAD && echo "SEAT FIX PRESENT" || echo "STOP: pull/rebase first"
```
If it says STOP, update commander-builder before running anything.

## Run command (24h, longer high-confidence sims)
```powershell
# ANTHROPIC_API_KEY MUST be empty -> curator bills against the Max subscription, not per-token
$env:ANTHROPIC_API_KEY = ""
python scripts/generate_sameprocess.py `
  --repo-dir <commander-builder> `
  --minutes 1440 `
  --sim-games 6 `
  --per-run-timeout 900 `
  --depths 3,5,7,9 `
  --seed 2
```
(The generator lives in the **commander-orchestrator** repo, `scripts/`. Machine 2
needs that repo checked out + its venv, with commander-builder pip-installed into it,
and Forge set up: `vendor/jre`, `vendor/forge`, decks under
`vendor/forge/userdata/decks/commander/`.)

### Why these values
- `--sim-games 6` + `--per-run-timeout 900`: measured pace is ~84s/game median,
  **127s/game p90**. 6 games on a slow deck ≈ 760s, so the timeout must be ≥900s —
  a strict 600s would kill the longer runs. 6 games resolves the near-tie noise that
  made 4-game margins mushy (regression needs clean margins).
- `--depths 3,5,7,9`: spread of detune depth → spread of margins. Deep depths (7,9)
  are what produce the **negative/reverted** margins the dataset currently lacks.
- **`--seed 2`** (Machine 1 uses `--seed 1`): different seeds → different deck/depth/
  filler choices → the two machines generate *diverse, non-duplicate* rows.

## Don't share one DB over the network
SQLite + two concurrent writers over a share = lock contention / corruption. Each
machine writes its **own local** `knowledge_log.sqlite`. Merge afterward:
1. Copy Machine 2's `knowledge_log.sqlite` over.
2. Append its rows into Machine 1's DB with **fresh ids** (don't collide on PK).
3. **Exclude every pre-seat-fix row** (on the canonical DB that's `id < 314`; on a
   fresh Machine-2 DB it's anything generated on pre-fix code — verify by date).
4. Dedup identical (deck, depth, seed) configs if any.

## Known gotchas (from the existing data)
- **Pending is ~1%** — not a problem. The few `pending` rows are `NULL sim_report`
  (sim skipped) on **B3** decks, likely a thin bracket-matched **filler pool**. If you
  see many B3 pendings, pass `--sim-fillers` to widen the pool.
- **"Celestial Tribunal"** crashed Forge once (exit 143 at ~138s) and went pending
  twice — a flaky/long deck. Not blocking; just don't be alarmed by occasional
  pendings on it.
- Generator is **resumable** (appends) — safe to restart after a reboot/crash.

## Expected yield
~6–10 trustworthy rows/hr/machine at 6-game sims → ~150–240 rows/machine over 24h.
Two machines comfortably clears the ~200 target for FP-002 with clean margins.
FP-013 (LoRA, 2000+) still needs many more machine-days — this gets FP-002 unblocked first.
