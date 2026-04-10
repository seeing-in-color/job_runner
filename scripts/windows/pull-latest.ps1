# Pull latest Job Runner code from git (run on the Windows machine that runs the CLI/UI).
# Usage (from repo root):  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\pull-latest.ps1
# Or from anywhere:        powershell -File C:\path\to\job_runner\scripts\windows\pull-latest.ps1 -RepoRoot C:\path\to\job_runner

param(
    [string] $RepoRoot = "",
    [string] $Branch = "main"
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

Set-Location $RepoRoot

Write-Host "Repo: $RepoRoot"
git fetch origin
git pull origin $Branch

Write-Host ""
Write-Host "Done. Restart anything that runs job_runner (UI terminal, scheduled task, etc.) so Python loads the new code."
Write-Host "Editable installs (pip install -e .) pick up changes after pull; restart is still required for a running process."
