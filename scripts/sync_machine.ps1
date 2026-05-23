<#
.SYNOPSIS
  Bring a soak-runner machine fully current: latest code + latest decks.

.DESCRIPTION
  Run on a secondary soak machine to sync with the source machine:
    1. Discards local edits (e.g. a local forge_runner timeout hardcode)
       so the committed, configurable version wins — keeps both machines
       on identical code.
    2. git pull --rebase the active branch.
    3. Copies any new / control decks the source staged in the shared
       inbox (\\<InboxHost>\soak_inbox\new_decks and \control_decks) into
       this machine's Forge deck dir.
  Does NOT restart the soak (so it won't clobber an in-flight run) — it
  prints the unified relaunch command at the end; run it when ready.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\sync_machine.ps1
  powershell -ExecutionPolicy Bypass -File scripts\sync_machine.ps1 -InboxHost 192.168.4.49
#>
param(
  [string]$InboxHost = "192.168.4.49",
  [string]$Branch = "feature/2026-04-28-session"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Write-Host "== sync_machine ==" -ForegroundColor Cyan

# 1. Discard local edits so committed code wins (uniform across machines).
$dirty = git status --porcelain
if ($dirty) {
  Write-Host "Discarding local edits to match committed code:`n$dirty" -ForegroundColor Yellow
  git checkout -- .
}

# 2. Latest code.
git fetch origin
git pull --rebase origin $Branch
Write-Host "code @ $(git rev-parse --short HEAD)" -ForegroundColor Green

# 3. Pull staged decks from the shared inbox.
$deckDir = "vendor\forge\userdata\decks\commander"
$copied = 0
foreach ($sub in @("new_decks", "control_decks")) {
  $src = "\\$InboxHost\soak_inbox\$sub"
  if (Test-Path $src) {
    $files = Get-ChildItem "$src\*.dck" -ErrorAction SilentlyContinue
    foreach ($f in $files) {
      # -LiteralPath is REQUIRED: deck filenames contain [ ] which Copy-Item
      # otherwise treats as wildcard char-classes, silently matching nothing
      # (it copied 0 files while reporting success — bug found by box2).
      Copy-Item -LiteralPath $f.FullName -Destination $deckDir -Force
      $copied++
    }
    if ($files) { Write-Host "copied $($files.Count) deck(s) from $sub" -ForegroundColor Green }
  }
}
Write-Host "decks synced: $copied new file(s)"

# Count [USER] base+v2 pairs. NB: glob bracket-escaping is unreliable in
# Windows PowerShell 5.1 (the old "[[]USER[]]*" pattern matched 0), so
# filter by name instead (fix reported by box2).
$nPairs = @(Get-ChildItem "$deckDir\*.dck" -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like '`[USER`]* v2 *.dck' }).Count
Write-Host "user v2 pairs now present: $nPairs"

Write-Host "`nSynced. To (re)launch on the unified config writing to the shared inbox:" -ForegroundColor Yellow
Write-Host "  # stop any running soak_pool + java first, then:" -ForegroundColor DarkGray
Write-Host "  .\.venv\Scripts\python.exe scripts\soak_pool.py --hours 24 --label box2b ``"
Write-Host "    --out \\$InboxHost\soak_inbox\box2b_throughput.jsonl ``"
Write-Host "    --summary \\$InboxHost\soak_inbox\box2b_summary.json"
