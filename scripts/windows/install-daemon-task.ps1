$ErrorActionPreference = "Stop"

$repo = "C:\Users\dreta\OneDrive\Documents\Coding\job_runner\job_runner"
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\windows\job_runner_daemon.py"
$watchdog = Join-Path $repo "scripts\windows\watchdog-daemon.ps1"
$taskName = "JobRunnerDaemon"
$watchdogTaskName = "JobRunnerDaemonWatchdog"

if (-not (Test-Path $py)) {
  throw "Python not found at $py"
}
if (-not (Test-Path $script)) {
  throw "Daemon script not found at $script"
}
if (-not (Test-Path $watchdog)) {
  throw "Watchdog script not found at $watchdog"
}

$taskCmd = "`"$py`" `"$script`""
$watchdogCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$watchdog`""

try {
  schtasks /Delete /TN $taskName /F | Out-Null
} catch {
}
try {
  schtasks /Delete /TN $watchdogTaskName /F | Out-Null
} catch {
}

schtasks /Create `
  /TN $taskName `
  /SC ONLOGON `
  /TR $taskCmd `
  /RL HIGHEST `
  /F | Out-Null

schtasks /Run /TN $taskName | Out-Null

schtasks /Create `
  /TN $watchdogTaskName `
  /SC MINUTE `
  /MO 1 `
  /TR $watchdogCmd `
  /RL HIGHEST `
  /F | Out-Null

schtasks /Run /TN $watchdogTaskName | Out-Null

Write-Output "Scheduled tasks '$taskName' and '$watchdogTaskName' installed and started."
