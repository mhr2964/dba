# Launcher for the headless ride-along test. Sets the env vars in one step.
# Usage from PowerShell:
#   cd C:\Users\Owner\Desktop\AI\Projects\dba
#   .\scripts\ride_along.ps1
#
# Optional flags get passed through, e.g.:
#   .\scripts\ride_along.ps1 --force-trades-interval 3

$env:DBA_HEADLESS_MODE = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
& "$projectRoot\.venv\Scripts\python.exe" "$projectRoot\scripts\headless_test.py" --force-trades --ride-along @args
