# Columnist ride-along launcher (PowerShell).
#
# Usage:
#   .\scripts\columnist_ride_along.ps1                     # interactive persona menu
#   .\scripts\columnist_ride_along.ps1 marcus_cole         # skip menu
#   .\scripts\columnist_ride_along.ps1 --list              # list valid persona ids and exit
#   .\scripts\columnist_ride_along.ps1 --bootstrap-existing <league_id>  # reuse a league
#
# Options:
#   --list                        Print valid persona ids one per line and exit.
#   --bootstrap-new               Create a fresh league (default).
#   --bootstrap-existing <id>     Reuse an existing league (must be REGULAR_SEASON_ACTIVE).
#   --sim-batch-size <N>          Batch size for each /sim run cycle (default: 20).

param(
    [Parameter(Position=0)]
    [string]$PersonaId = "",

    [switch]$List,
    [switch]$BootstrapNew,
    [string]$BootstrapExisting = "",
    [int]$SimBatchSize = 20
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = "$projectRoot\.venv\Scripts\python.exe"

# ── Guard: check for conflicting env vars ────────────────────────────────────
if ($env:DBA_RIDE_ALONG -eq "1") {
    Write-Error "ERROR: DBA_RIDE_ALONG=1 is already set. Cannot run columnist ride-along simultaneously with trade ride-along (stdin collision). Unset DBA_RIDE_ALONG first."
    exit 1
}

if ($env:DBA_HEADLESS_MODE -eq "1") {
    Write-Error "ERROR: DBA_HEADLESS_MODE=1 is set. Columnist ride-along requires a real Discord bot connection. Unset DBA_HEADLESS_MODE first."
    exit 1
}

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "ERROR: ANTHROPIC_API_KEY is not set. Columnist articles cannot be generated without it."
    exit 1
}

# ── Load PERSONAS from Python (via helper script) ─────────────────────────────
# Write a small temp script to avoid PowerShell string-interpolation conflicts.
$tmpScript = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $tmpScript -Value @"
import sys, pathlib
sys.path.insert(0, r"$projectRoot")
from dotenv import load_dotenv
load_dotenv(pathlib.Path(r"$projectRoot") / ".env")
from services.personas import PERSONAS
for pid, p in sorted(PERSONAS.items()):
    print(pid + "|" + p.display_name)
"@

$personaLines = & $python $tmpScript 2>$null
Remove-Item $tmpScript -ErrorAction SilentlyContinue

if ($LASTEXITCODE -ne 0 -or -not $personaLines) {
    Write-Error "ERROR: Failed to load PERSONAS from services/personas/__init__.py."
    exit 1
}

# Build ordered list: [[id, display_name], ...]
$personaList = @()
foreach ($line in $personaLines) {
    $parts = $line -split '\|', 2
    if ($parts.Count -eq 2) {
        $personaList += ,@($parts[0].Trim(), $parts[1].Trim())
    }
}

# ── --list flag ───────────────────────────────────────────────────────────────
if ($List) {
    foreach ($p in $personaList) {
        Write-Host $p[0]
    }
    exit 0
}

# ── Resolve persona id ────────────────────────────────────────────────────────
$validIds = $personaList | ForEach-Object { $_[0] }

if (-not $PersonaId) {
    # Interactive menu
    Write-Host ""
    Write-Host "Columnist Ride-Along -- pick a persona:"
    Write-Host ""
    for ($i = 0; $i -lt $personaList.Count; $i++) {
        $num = "{0,2}" -f ($i + 1)
        $id  = "{0,-22}" -f $personaList[$i][0]
        Write-Host ("  $num. $id " + $personaList[$i][1])
    }
    Write-Host ""
    $choice = Read-Host "Enter number or persona id"
    $choice = $choice.Trim()

    # Try numeric
    $choiceInt = 0
    if ([int]::TryParse($choice, [ref]$choiceInt)) {
        $idx = $choiceInt - 1
        if ($idx -ge 0 -and $idx -lt $personaList.Count) {
            $PersonaId = $personaList[$idx][0]
        } else {
            Write-Error ("ERROR: number $choiceInt is out of range (1.." + $personaList.Count + ").")
            exit 1
        }
    } else {
        $PersonaId = $choice
    }
}

# Validate
if ($validIds -notcontains $PersonaId) {
    $idList = $validIds -join "`n  "
    Write-Error "ERROR: '$PersonaId' is not a valid persona id.`nValid ids:`n  $idList"
    exit 1
}

Write-Host ""
Write-Host ("columnist_ride_along: Target persona: " + $PersonaId)
Write-Host ""

# ── Bootstrap ─────────────────────────────────────────────────────────────────
$leagueId = ""

if ($BootstrapExisting) {
    Write-Host ("columnist_ride_along: Reusing league id=" + $BootstrapExisting + "...")
    $bootstrapOut = & $python "$projectRoot\scripts\_columnist_ra_bootstrap.py" --reuse-league $BootstrapExisting 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("Bootstrap failed:`n" + $bootstrapOut)
        exit 1
    }
} else {
    # Default: --new
    Write-Host "columnist_ride_along: Creating fresh league (this may take ~30s)..."
    $bootstrapOut = & $python "$projectRoot\scripts\_columnist_ra_bootstrap.py" --new 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("Bootstrap failed:`n" + $bootstrapOut)
        exit 1
    }
}

Write-Host $bootstrapOut

# Extract LEAGUE_ID from output
foreach ($line in ($bootstrapOut -split "`n")) {
    if ($line -match '^LEAGUE_ID=(\d+)') {
        $leagueId = $Matches[1]
    }
}

if (-not $leagueId) {
    Write-Error "ERROR: Bootstrap did not print a LEAGUE_ID. Check output above."
    exit 1
}

Write-Host ("columnist_ride_along: League id: " + $leagueId)
Write-Host "columnist_ride_along: Starting bot (run.py)..."
Write-Host "columnist_ride_along: Type feedback at the pause prompt."
Write-Host "columnist_ride_along: Type :quit to stop cleanly."
Write-Host ""

# ── Launch bot with ride-along env vars set ───────────────────────────────────
$env:DBA_COLUMNIST_RIDE_ALONG = $PersonaId

Set-Location $projectRoot
& $python "$projectRoot\run.py"

# ── Post-exit summary ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "columnist_ride_along: Bot exited. Printing session summary..."

$summaryScript = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $summaryScript -Value @"
import sys, pathlib, os
sys.path.insert(0, r"$projectRoot")
from dotenv import load_dotenv
load_dotenv(pathlib.Path(r"$projectRoot") / ".env")
os.environ["DBA_COLUMNIST_RIDE_ALONG"] = "$PersonaId"
from services.columnist_ride_along import summarize_session
summarize_session()
"@
& $python $summaryScript
Remove-Item $summaryScript -ErrorAction SilentlyContinue
