<#
.SYNOPSIS
  Tiny file-based mailbox over the shared soak inbox so the soak machines
  (box1 / box2 / ...) can pass messages without any server.

.DESCRIPTION
  Messages are plain markdown files under \\<InboxHost>\soak_inbox\msgs\ named
    <unixtime>_<from>_to_<to>.md
  'send' writes one. 'read -Me <name>' prints every message addressed to you
  (or to 'all') oldest-first, then files them under msgs\_read\ so the next
  read won't repeat them (pass -KeepRead to leave them unread). 'list' shows
  all pending messages without consuming them.

  ASCII-only and uses UTF-8 output so it parses on stock Windows PowerShell 5.1.

.EXAMPLE
  # box1 sends to box2:
  powershell -File scripts\soak_msg.ps1 -Action send -From box1 -To box2 -Body "pulled your decks, thanks"
  # box1 sends a longer note from a file:
  powershell -File scripts\soak_msg.ps1 -Action send -From box1 -To box2 -BodyFile note.md
  # box2 reads its mail:
  powershell -File scripts\soak_msg.ps1 -Action read -Me box2
  # peek without consuming:
  powershell -File scripts\soak_msg.ps1 -Action list
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('send', 'read', 'list')][string]$Action,
  [string]$From,
  [string]$To = 'all',
  [string]$Body,
  [string]$BodyFile,
  [string]$Me,
  [string]$InboxHost = '192.168.4.49',
  [switch]$KeepRead
)
$ErrorActionPreference = 'Stop'
$root = "\\$InboxHost\soak_inbox\msgs"
$readDir = Join-Path $root '_read'
New-Item -ItemType Directory -Path $root -Force | Out-Null

function Get-Token([string]$s) { ($s -replace '[^A-Za-z0-9]', '').ToLower() }

switch ($Action) {
  'send' {
    if (-not $From) { throw "send needs -From <name>" }
    $text = if ($BodyFile) { Get-Content -LiteralPath $BodyFile -Raw } elseif ($Body) { $Body } else { throw "send needs -Body or -BodyFile" }
    $f = Get-Token $From; $t = Get-Token $To
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $name = "${ts}_${f}_to_${t}.md"
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss') + ' UTC'
    $full = "# $From -> $To   ($stamp)`n`n$text`n"
    Set-Content -Path (Join-Path $root $name) -Value $full -Encoding utf8
    Write-Host "sent: $name" -ForegroundColor Green
  }
  'list' {
    $msgs = Get-ChildItem -LiteralPath $root -Filter '*.md' -ErrorAction SilentlyContinue | Sort-Object Name
    if (-not $msgs) { Write-Host "(no pending messages)"; break }
    foreach ($m in $msgs) { Write-Host ("{0}   [{1}]" -f $m.Name, (Get-Content -LiteralPath $m.FullName -TotalCount 1)) }
  }
  'read' {
    if (-not $Me) { throw "read needs -Me <name>" }
    $me = Get-Token $Me
    $msgs = Get-ChildItem -LiteralPath $root -Filter '*.md' -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match "_to_(${me}|all)\.md$" } | Sort-Object Name
    if (-not $msgs) { Write-Host "(no messages for $Me)"; break }
    if (-not $KeepRead) { New-Item -ItemType Directory -Path $readDir -Force | Out-Null }
    foreach ($m in $msgs) {
      Write-Host ("=" * 70)
      Get-Content -LiteralPath $m.FullName | Write-Host
      if (-not $KeepRead) { Move-Item -LiteralPath $m.FullName -Destination (Join-Path $readDir $m.Name) -Force }
    }
    Write-Host ("=" * 70)
    if (-not $KeepRead) { Write-Host ("filed {0} message(s) under _read\" -f $msgs.Count) -ForegroundColor DarkGray }
  }
}
