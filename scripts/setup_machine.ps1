<#
.SYNOPSIS
  Turnkey setup for a Forge sim-soak runner machine (Windows). Idempotent.

.DESCRIPTION
  Sets up a freshly-cloned commander-builder checkout to run
  scripts/soak_pool.py: creates a venv, installs the package + psutil,
  verifies the (gitignored) Forge runtime was copied in, and materializes
  the concurrent Forge profiles. Optionally launches the soak.

  PREREQUISITE you must do by hand first (these are gitignored, ~592 MB):
    copy  vendor\forge  (446 MB: jar + res game-data + the 188 decks)
    copy  vendor\jre    (146 MB: bundled Java)
  from the source machine into this checkout's .\vendor\ directory.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup_machine.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup_machine.ps1 -Launch
#>
param(
  [int]$Profiles = 12,
  [switch]$Launch,
  [double]$Hours = 24,
  # Operator directive (soak-sims.md): always 40-game sims for high-confidence
  # verdicts — never 5. phase2-after defaults to 999999 so the soak stays at 40
  # throughout (no fast phase-1 fallback to a lower game count).
  [int]$Games = 40,
  [int]$Phase2Games = 40,
  [int]$Phase2After = 999999,
  # Concurrency. Defaults match this hardware's sweet spot: 12 physical cores.
  # Benchmark (2026-05-26, Ryzen 9 3900X 12c/24t): a fixed sim workload finished
  # no faster at 24 workers than 12 (293s vs 292s) because SMT hyperthreads don't
  # help CPU-bound Forge JVMs. Past soaks ran --max 6 (~48% CPU = half idle);
  # 12 is ~1.6x the throughput with no contention penalty. Don't exceed physical
  # cores. --start at --max so the soak begins saturated instead of climbing.
  [int]$Min = 4,
  [int]$Max = 12,
  [int]$Start = 12
)
if ($Start -gt $Max) { $Start = $Max }
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot   # scripts\ -> repo root
Set-Location $repo
Write-Host "== commander-builder soak setup ==" -ForegroundColor Cyan
Write-Host "Repo: $repo"

# 1. Python interpreter (prefer the py launcher, then python on PATH).
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { throw "Python not found. Install Python 3.10+ (with the 'py' launcher) and retry." }

if (-not (Test-Path ".venv")) {
  Write-Host "Creating .venv ..."
  & $py.Source -m venv .venv
}
$venvPy = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv python missing at $venvPy" }
Write-Host "Installing package + deps (editable) ..."
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -e . psutil

# 2. Verify the copied Forge runtime (gitignored - must be present).
$jar = Get-ChildItem "vendor\forge\forge-gui-desktop-*.jar" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jar) {
  throw "MISSING vendor\forge\forge-gui-desktop-*.jar. Copy vendor\forge (446MB) from the source machine into .\vendor\ and re-run."
}
if (-not (Test-Path "vendor\jre\bin\java.exe")) {
  throw "MISSING vendor\jre\bin\java.exe. Copy vendor\jre (146MB) into .\vendor\ and re-run."
}
$deckCount = (Get-ChildItem "vendor\forge\userdata\decks\commander\*.dck" -ErrorAction SilentlyContinue).Count
if ($deckCount -lt 2) { throw "Only $deckCount decks under vendor\forge\userdata\decks\commander - the copy is incomplete." }
Write-Host "Forge OK: $($jar.Name) | $deckCount decks" -ForegroundColor Green

# 3. Create cwd-isolated profiles forge2..N (cheap junctions; idempotent).
Write-Host "Creating $Profiles Forge profiles ..."
for ($i = 2; $i -le $Profiles; $i++) {
  & $venvPy scripts\setup_forge_profile.py vendor\forge "vendor\forge$i" | Out-Null
}
$nProf = (Get-ChildItem -Directory "vendor\forge*").Count
Write-Host "Profiles ready: $nProf (forge + forge2..$Profiles)" -ForegroundColor Green

# 4. Launch (optional) or print the command.
$cmd = ".\.venv\Scripts\python.exe scripts\soak_pool.py --hours $Hours --min $Min --max $Max --start $Start --games $Games --phase2-games $Phase2Games --phase2-after $Phase2After"
if ($Launch) {
  Write-Host "Launching soak (detached) at min $Min / max $Max / start $Start runners ..." -ForegroundColor Cyan
  Start-Process -FilePath $venvPy `
    -ArgumentList @("scripts\soak_pool.py","--hours",$Hours,
                    "--min",$Min,"--max",$Max,"--start",$Start,"--games",$Games,
                    "--phase2-games",$Phase2Games,"--phase2-after",$Phase2After) `
    -RedirectStandardOutput "soak_run.log" -RedirectStandardError "soak_run.err.log" `
    -WindowStyle Hidden
  Start-Sleep 4
  Write-Host "Launched. Live summary at: $((Join-Path $HOME 'soak_summary.json'))" -ForegroundColor Green
  Write-Host "Per-batch log: $repo\soak_run.log"
} else {
  Write-Host "`nSetup complete. To launch the 24h soak:`n  $cmd" -ForegroundColor Yellow
}
