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
  [int]$Games = 5,
  [int]$Phase2Games = 40,
  [int]$Phase2After = 200
)
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

# 2. Verify the copied Forge runtime (gitignored — must be present).
$jar = Get-ChildItem "vendor\forge\forge-gui-desktop-*.jar" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jar) {
  throw "MISSING vendor\forge\forge-gui-desktop-*.jar. Copy vendor\forge (446MB) from the source machine into .\vendor\ and re-run."
}
if (-not (Test-Path "vendor\jre\bin\java.exe")) {
  throw "MISSING vendor\jre\bin\java.exe. Copy vendor\jre (146MB) into .\vendor\ and re-run."
}
$deckCount = (Get-ChildItem "vendor\forge\userdata\decks\commander\*.dck" -ErrorAction SilentlyContinue).Count
if ($deckCount -lt 2) { throw "Only $deckCount decks under vendor\forge\userdata\decks\commander — the copy is incomplete." }
Write-Host "Forge OK: $($jar.Name) | $deckCount decks" -ForegroundColor Green

# 3. Create cwd-isolated profiles forge2..N (cheap junctions; idempotent).
Write-Host "Creating $Profiles Forge profiles ..."
for ($i = 2; $i -le $Profiles; $i++) {
  & $venvPy scripts\setup_forge_profile.py vendor\forge "vendor\forge$i" | Out-Null
}
$nProf = (Get-ChildItem -Directory "vendor\forge*").Count
Write-Host "Profiles ready: $nProf (forge + forge2..$Profiles)" -ForegroundColor Green

# 4. Launch (optional) or print the command.
$cmd = ".\.venv\Scripts\python.exe scripts\soak_pool.py --hours $Hours --games $Games --phase2-games $Phase2Games --phase2-after $Phase2After"
if ($Launch) {
  Write-Host "Launching soak (detached) ..." -ForegroundColor Cyan
  Start-Process -FilePath $venvPy `
    -ArgumentList @("scripts\soak_pool.py","--hours",$Hours,"--games",$Games,
                    "--phase2-games",$Phase2Games,"--phase2-after",$Phase2After) `
    -RedirectStandardOutput "soak_run.log" -RedirectStandardError "soak_run.err.log" `
    -WindowStyle Hidden
  Start-Sleep 4
  Write-Host "Launched. Live summary at: $((Join-Path $HOME 'soak_summary.json'))" -ForegroundColor Green
  Write-Host "Per-batch log: $repo\soak_run.log"
} else {
  Write-Host "`nSetup complete. To launch the 24h soak:`n  $cmd" -ForegroundColor Yellow
}
