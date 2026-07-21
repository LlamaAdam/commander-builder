<#
.SYNOPSIS
  Continuously publish this machine's local soak output to the shared inbox
  so the other machine can see our progress (and merge it).

.DESCRIPTION
  box1 writes its soak files to %USERPROFILE% (local + session folder),
  while box2 writes straight to the share — so box2 was blind to box1's
  run. This copies <Label>_throughput.jsonl + <Label>_summary.json into
  \soak_inbox every -IntervalSec. Uses -LiteralPath (bracket-safe). Run
  detached for the life of the soak.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish_soak.ps1 -Label Llama
#>
param(
  [string]$Label = $env:COMPUTERNAME,
  [int]$IntervalSec = 60,
  [string]$InboxHost = "192.168.4.49"
)
$src = $env:USERPROFILE
$dst = "\\$InboxHost\soak_inbox"
Write-Host "publishing $Label soak -> $dst every ${IntervalSec}s (Ctrl-C to stop)"
while ($true) {
  foreach ($pair in @(
      @("$src\soak_throughput.jsonl", "$dst\${Label}_throughput.jsonl"),
      @("$src\soak_summary.json",     "$dst\${Label}_summary.json"))) {
    try {
      if (Test-Path -LiteralPath $pair[0]) {
        Copy-Item -LiteralPath $pair[0] -Destination $pair[1] -Force -ErrorAction SilentlyContinue
      }
    } catch {}
  }
  Start-Sleep -Seconds $IntervalSec
}
