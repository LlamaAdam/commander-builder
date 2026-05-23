# Soak Runner — Handoff Runbook

> **Paste this whole file into Claude Code on the new machine.** It tells
> the assistant exactly how to stand up a Forge sim-soak runner and start
> generating knowledge_log data in parallel with the main machine.

---

## Context (what you're setting up)

`commander-builder` runs Forge A/B sims (deck vs its curated `v2`) to
accumulate knowledge_log rows for the FP-002 / FP-013 data gates. Sims are
**embarrassingly parallel** — each machine runs independently and the
JSONL outputs are merged later (no coordination, no shared DB). This
machine is a **second worker**: run the soak, then ship its JSONL back.

Measured throughput on the source box (Ryzen 9 3900X, 12c/24t): **~200
games/hr, ~40 rows/hr at `games=5`** — CPU-bound. Yours scales with its
own core count.

## Prerequisites (do these BEFORE asking Claude Code to run)

1. **Windows**, Python 3.10+ (with the `py` launcher).
2. **The Forge runtime** (`vendor\forge` + `vendor\jre`, ~592 MB) is
   gitignored, so a clone doesn't include it. It's published as a
   **GitHub Release asset** — Claude Code downloads it in step 2 below.
   No flash drive / manual copy needed. Direct URL:
   `https://github.com/LlamaAdam/commander-builder/releases/download/soak-runtime/soak_runtime.tar.gz`

## Instructions for Claude Code (run these)

```
1. Confirm you're in the commander-builder repo root (it has pyproject.toml
   and scripts\soak_pool.py). If not cloned yet:
     git clone https://github.com/LlamaAdam/commander-builder
     cd commander-builder

2. Download + extract the Forge runtime (gitignored; ~350 MB compressed
   from the GitHub Release). From the repo root (the `vendor` dir already
   exists in a clone — it tracks vendor/README.md):
     curl -L -o soak_runtime.tar.gz https://github.com/LlamaAdam/commander-builder/releases/download/soak-runtime/soak_runtime.tar.gz
     tar -xf soak_runtime.tar.gz -C vendor
   (`tar -xf` on a .tar.gz works in both PowerShell and bash on Windows.)
   Then verify:
     - vendor\forge\forge-gui-desktop-*.jar exists
     - vendor\jre\bin\java.exe exists
     - vendor\forge\userdata\decks\commander\*.dck is ~188 files
   If the download fails, STOP and tell me — do not proceed without the runtime.

3. Run the turnkey setup (creates venv, installs deps, makes 12 Forge
   profiles, launches the 24h two-phase soak):
     powershell -ExecutionPolicy Bypass -File scripts\setup_machine.ps1 -Launch

   That runs: games=5 until 200 rows banked, then auto-switches to
   games=40 for high-confidence verdicts.

4. Confirm it's alive:
     - a python.exe running scripts\soak_pool.py
     - several java.exe processes (the concurrent Forge sims)
     - the live summary file at  %USERPROFILE%\soak_summary.json
       (it shows active_runners, cpu_pct, sims_done, games_per_hour).
   First completed sims take ~7-10 min; until then games_per_hour is 0
   but CPU should be high — that's normal (sims in flight).

5. Let it run. Every ~20s it rewrites %USERPROFILE%\soak_summary.json and
   appends to %USERPROFILE%\soak_throughput.jsonl.
```

## Healthy signature (so it doesn't look broken)
- **High CPU, low RAM, idle GPU** is correct — Forge is CPU-only Java.
- `sims_done = 0` for the first several minutes is normal (sims in flight).
- The autoscaler holds CPU ~78–92%, adding/removing runners (4–12).

## Sending results back / merging
The output is `%USERPROFILE%\soak_throughput.jsonl`. Copy it to the source
machine and merge (rows are independent — just concatenate):
```
python scripts\merge_soak.py path\to\machine1.jsonl path\to\machine2.jsonl
# add --to-knowledge-log to fold completed sims into knowledge_log:
python scripts\merge_soak.py *.jsonl --to-knowledge-log
```

## Knobs (optional)
`scripts\soak_pool.py` flags: `--hours`, `--games`, `--phase2-games`,
`--phase2-after`, `--min`/`--max`/`--start` (runner bounds),
`--cpu-low`/`--cpu-high` (autoscale band), `--out`/`--summary` (paths —
default `%USERPROFILE%\soak_*`). `setup_machine.ps1` mirrors the soak knobs
plus `-Profiles N` and `-Launch`.

## Stop it
```
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  ? { $_.CommandLine -like '*soak_pool*' } | % { Stop-Process -Id $_.ProcessId -Force }
Get-Process java -ErrorAction SilentlyContinue | Stop-Process -Force
```
