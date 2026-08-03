# Soak Runner — Handoff Runbook

> **Paste this whole file into Claude Code on the new machine.** It tells
> the assistant exactly how to stand up a Forge sim-soak runner and start
> generating soak rows in parallel with the main machine.

---

## Context (what you're setting up)

`commander-builder` runs Forge sims to accumulate win-rate rows. The
current convention is **gauntlet mode**: each test deck (base and its
` v2 `) plays individually against a FIXED, committed 3-deck gauntlet
(the MH3 Commander precons — see `GAUNTLET` in `scripts/soak_pool.py`),
so a base-vs-v2 win-rate delta is attributable to the deck edit alone,
not a shifting field. Every machine MUST run the identical gauntlet for
merged verdicts to be valid. Gauntlet rows carry their own schema
(`mode: "gauntlet"`, `test_deck` / `role` / `pair_base` / `gauntlet`)
and live in separate files from the old ab-mode rows — don't mix them.

Sims are **embarrassingly parallel** — each machine runs independently
and the JSONL outputs are merged later (no coordination, no shared DB).

**Game count: always `--games 40`.** Operator directive — 40-game sims
give high-confidence verdicts; never run `--games 5` again (we have
plenty of low-confidence rows). `setup_machine.ps1` already defaults to
40 with the phase-2 downshift disabled.

**Restarts: always `--append`.** Without it the soak truncates `--out`
on start and throws away every banked row.

## Prerequisites (do these BEFORE asking Claude Code to run)

1. **Windows**, Python 3.10+ (with the `py` launcher).
2. **The Forge runtime** (`vendor\forge` + `vendor\jre`, ~592 MB) is
   gitignored, so a clone doesn't include it. It's published as a
   **GitHub Release asset** — Claude Code downloads it in step 2 below.
   No flash drive / manual copy needed. Direct URL:
   `https://github.com/LlamaAdam/commander-builder/releases/download/soak-runtime/soak_runtime.tar.gz`
3. **The shared inbox** `\\LLAMA\soak_inbox` is reachable (hostname, not
   IP — DHCP has moved the host before, `\\LLAMA` is stable).

## Instructions for Claude Code (run these)

```
1. Confirm you're in the commander-builder repo root (it has pyproject.toml
   and scripts\soak_pool.py). If not cloned yet:
     git clone https://github.com/LlamaAdam/commander-builder
     cd commander-builder
   If already cloned, sync code + staged decks from the source machine:
     powershell -ExecutionPolicy Bypass -File scripts\sync_machine.ps1 -Branch master
   (defaults to -InboxHost LLAMA; it does NOT restart a running soak —
   it prints the relaunch command instead.)

2. Download + extract the Forge runtime (gitignored; ~350 MB compressed
   from the GitHub Release). From the repo root (the `vendor` dir already
   exists in a clone — it tracks vendor/README.md):
     curl -L -o soak_runtime.tar.gz https://github.com/LlamaAdam/commander-builder/releases/download/soak-runtime/soak_runtime.tar.gz
     tar -xf soak_runtime.tar.gz -C vendor
   (`tar -xf` on a .tar.gz works in both PowerShell and bash on Windows.)
   Then verify:
     - vendor\forge\forge-gui-desktop-*.jar exists
     - vendor\jre\bin\java.exe exists
     - vendor\forge\userdata\decks\commander\*.dck is ~188 files, and the
       3 gauntlet decks are among them (Eldrazi Incursion / Graveyard
       Overdrive / Creative Energy, all "[M3C] [2024]")
   If the download fails, STOP and tell me — do not proceed without the runtime.

3. Turnkey environment setup (venv, deps, 12 cwd-isolated Forge profiles):
     powershell -ExecutionPolicy Bypass -File scripts\setup_machine.ps1
   Then launch the gauntlet soak (setup_machine's -Launch starts an
   ab-mode soak; the current convention is gauntlet, launched directly):
     .\.venv\Scripts\python.exe scripts\soak_pool.py --mode gauntlet ^
         --games 40 --append --hours 24 --min 4 --max 12 --start 12
   Add COMMANDER_BUILDER_KEEP_GAME_LOGS=1 in the environment first if
   this run should also capture replay-lite game logs (FP-016 turn-by-turn
   replays; default off — logs are discarded unless the flag is set).

4. Confirm it's alive:
     - a python.exe running scripts\soak_pool.py
     - several java.exe processes (the concurrent Forge sims)
     - the live summary file at  %USERPROFILE%\soak_summary.json
       (it shows active_runners, cpu_pct, sims_done, games_per_hour).
   A 40-game sim takes a while; until the first one completes,
   games_per_hour is 0 but CPU should be high — that's normal.

5. Let it run. Every ~20s it rewrites %USERPROFILE%\soak_summary.json and
   appends to %USERPROFILE%\soak_throughput.jsonl.
```

## Healthy signature (so it doesn't look broken)
- **High CPU, low RAM, idle GPU** is correct — Forge is CPU-only Java.
- `sims_done = 0` for a long first stretch is normal (40-game sims in
  flight).
- The autoscaler holds CPU ~78–92%, adding/removing runners (4–12).
- **Failure storms are contained at source** (`StormBreaker` in
  `soak_pool.py`): instant launch failures back off per-runner, ~10
  consecutive ones open a circuit breaker (canary probe every 15 min),
  and while it's open at most a handful of failure rows are written plus
  periodic `storm_suppressed` summary rows. If you see those rows, the
  pool is protecting the data file — investigate the launch failure, do
  not delete the breaker.

## Sending results back / merging (tracked separate, summed together)

Each machine's rows live in its own `%USERPROFILE%\soak_throughput.jsonl`
and carry a `host` tag (`--label`, default hostname), so provenance is
preserved.

To publish progress continuously to the shared inbox, run detached for
the life of the soak:
```
powershell -ExecutionPolicy Bypass -File scripts\publish_soak.ps1 -Label <box>
```
This copies the LOCAL files to `\\LLAMA\soak_inbox\<Label>_*.jsonl/json`
every 60 s.

> **FOOTGUN — never run publish_soak.ps1 when the soak's `--out` already
> points at the share.** publish_soak copies
> `%USERPROFILE%\soak_throughput.jsonl` OVER
> `\\LLAMA\soak_inbox\<Label>_throughput.jsonl`. If the soak is writing
> its rows directly to that share path, the copy clobbers the live rows
> with a stale (or empty) local file. Either the soak writes locally and
> publish_soak ships it, or the soak writes to the share and nothing
> else touches that file — never both.

Merge on the source box with a **label per machine** — the merger prints
a per-source breakdown AND the combined total:
```
python scripts\merge_soak.py box1=C:\Users\pilot\soak_throughput.jsonl box2=\\LLAMA\soak_inbox\box2_throughput.jsonl
#   source            rows   done   games
#   box1               260    258    1290
#   box2               240    239    1195
#   TOTAL              500    497    2485
# add --to-knowledge-log to fold completed sims into knowledge_log
# (source/host kept in each row's manifest):
python scripts\merge_soak.py box1=... box2=... --to-knowledge-log
```

## Mailbox (cross-machine messages, no server)

`\\LLAMA\soak_inbox\msgs\` is a file mailbox driven by
`scripts\soak_msg.ps1` (`-Action send|read|list`). Convention: **the
reader archives** — `read -Me <box>` prints your mail then files it
under `msgs\_read\` so the next read won't repeat it (`-KeepRead` to
peek without consuming; `list` shows pending mail for everyone without
consuming it). Senders never move messages.
```
powershell -File scripts\soak_msg.ps1 -Action send -From box2 -To box1 -Body "40-game gauntlet soak relaunched"
powershell -File scripts\soak_msg.ps1 -Action read -Me box2
```

## Knobs (optional)

`scripts\soak_pool.py` flags: `--mode ab|gauntlet`, `--hours`, `--games`
(**always 40** — see directive above), `--phase2-games`/`--phase2-after`
(legacy two-phase downshift; leave disabled), `--append` (**always on
restarts**), `--label` (provenance host tag),
`--min`/`--max`/`--start` (runner bounds), `--cpu-low`/`--cpu-high`
(autoscale band), `--timeout` (per-game Forge timeout),
`--out`/`--summary` (paths — default `%USERPROFILE%\soak_*`).
`setup_machine.ps1` mirrors the soak knobs plus `-Profiles N` and
`-Launch`.

Keep gauntlet-mode output in its own file if the machine still has old
ab-mode rows — the schemas differ and downstream analysis filters by
`mode`.

## Stop it
```
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  ? { $_.CommandLine -like '*soak_pool*' } | % { Stop-Process -Id $_.ProcessId -Force }
Get-Process java -ErrorAction SilentlyContinue | Stop-Process -Force
```

## Hung-loop rows

`loop_unattributed` rows (and pre-2026-07-24 rows marked `done` with a
"loop at game N" error) are a known, benign censoring mode — see
[docs/loop-rate-investigation-2026-08.md](loop-rate-investigation-2026-08.md)
for the two-marker fold, rates, and how to inspect individual hung games
via replay capture.
