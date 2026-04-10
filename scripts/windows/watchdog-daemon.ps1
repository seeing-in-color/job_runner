$ErrorActionPreference = "SilentlyContinue"

$repo = "C:\Users\dreta\OneDrive\Documents\Coding\job_runner\job_runner"
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$script = Join-Path $repo "scripts\windows\job_runner_daemon.py"

if (-not (Test-Path $py) -or -not (Test-Path $script)) {
  exit 0
}

$needle = "job_runner_daemon.py"
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$needle*" }

if ($procs.Count -gt 1) {
  $keep = $procs | Sort-Object CreationDate | Select-Object -First 1
  $drop = $procs | Where-Object { $_.ProcessId -ne $keep.ProcessId }
  foreach ($p in $drop) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  exit 0
}

if ($procs.Count -eq 0) {
  Start-Process -FilePath $py -ArgumentList "`"$script`"" -WorkingDirectory $repo -WindowStyle Hidden | Out-Null
}
