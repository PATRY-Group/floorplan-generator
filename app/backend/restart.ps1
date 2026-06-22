# Restart the floorplans backend cleanly.
# Kills the whole uvicorn --reload process tree for THIS project (launcher +
# reload supervisor + the spawned worker grandchild that, on Windows, often
# survives an edit and keeps serving stale code), then relaunches fresh.
# Run from anywhere:  powershell -File app\backend\restart.ps1

$ErrorActionPreference = 'SilentlyContinue'

# Match every python/uvicorn process whose command line points at this backend,
# so we never touch other projects (e.g. the content-gen server on :8010).
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uvicorn.exe'" |
    Where-Object { $_.CommandLine -like '*floorplans*backend*' }

if ($procs) {
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Milliseconds 600
    Write-Host "Stopped $($procs.Count) stale backend process(es)." -ForegroundColor Yellow
} else {
    Write-Host "No running backend found." -ForegroundColor DarkGray
}

# Launch fresh from the backend dir using its own venv.
Set-Location $PSScriptRoot
Write-Host "Starting backend on http://localhost:8000 ..." -ForegroundColor Green
& "$PSScriptRoot\.venv\Scripts\uvicorn.exe" main:app --reload --port 8000
