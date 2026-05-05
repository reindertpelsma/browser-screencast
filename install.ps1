param(
  [switch]$Start,
  [switch]$ScheduledTask,
  [switch]$NoDeps
)

$ErrorActionPreference = "Stop"

if ($IsMacOS) {
  Write-Host "macOS detected. Use macscreencast instead:"
  Write-Host "  https://github.com/reindertpelsma/macscreencast"
  exit 0
}

$InstallDir = if ($env:BROWSER_SCREENCAST_HOME) { $env:BROWSER_SCREENCAST_HOME } else { Join-Path $env:LOCALAPPDATA "browser-screencast" }
$BinDir = if ($env:BROWSER_SCREENCAST_BIN) { $env:BROWSER_SCREENCAST_BIN } else { Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps" }
$ConfigDir = Join-Path $env:APPDATA "browser-screencast"
$Port = if ($env:PORT) { $env:PORT } else { "6081" }

New-Item -ItemType Directory -Force -Path $InstallDir, $BinDir, $ConfigDir | Out-Null

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $Here "server.py")) {
  Copy-Item -Recurse -Force (Join-Path $Here "*") $InstallDir
} else {
  throw "Run install.ps1 from a browser-screencast checkout for now."
}

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
if (-not $NoDeps) {
  & $Python -m pip install --user -r (Join-Path $InstallDir "requirements.txt")
}

$TokenFile = Join-Path $ConfigDir "token"
if (-not (Test-Path $TokenFile)) {
  $Token = & $Python -c "import secrets; print(secrets.token_urlsafe(24))"
  Set-Content -NoNewline -Path $TokenFile -Value $Token
} else {
  $Token = Get-Content -Raw $TokenFile
}

$CmdPath = Join-Path $BinDir "browser-screencast.cmd"
Set-Content -Path $CmdPath -Value "@echo off`r`n`"$Python`" `"$InstallDir\server.py`" --password `"$Token`" %*`r`n"

if ($ScheduledTask) {
  $Action = New-ScheduledTaskAction -Execute $CmdPath -Argument "--listen 127.0.0.1 --port $Port"
  $Trigger = New-ScheduledTaskTrigger -AtLogOn
  Register-ScheduledTask -TaskName "browser-screencast" -Action $Action -Trigger $Trigger -Description "browser-screencast server" -Force | Out-Null
  Start-ScheduledTask -TaskName "browser-screencast"
}

Write-Host ""
Write-Host "Installed browser-screencast."
Write-Host "Token:  $Token"
Write-Host "Run:    $CmdPath --port $Port"
Write-Host "SSH:    ssh -L ${Port}:localhost:${Port} user@<host>"
Write-Host "Open:   http://localhost:${Port}/?token=$Token"
Write-Host "Task:   .\install.ps1 -ScheduledTask"

if ($Start) {
  & $CmdPath --port $Port
}
