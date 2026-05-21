<#
.SYNOPSIS
    Columnist ride-along v2 sidecar CLI (PowerShell).

.DESCRIPTION
    Attaches to a running DBA bot via file IPC and pauses the sim every time
    the chosen persona posts an article, prompting for free-form feedback that
    is written to a JSONL log.

.EXAMPLE
    pwsh .\scripts\columnist_feedback.ps1 marcus_cole
    pwsh .\scripts\columnist_feedback.ps1          # interactive persona menu
    pwsh .\scripts\columnist_feedback.ps1 --list   # print valid persona ids; exit
#>
param(
    [string]$PersonaArg = "",
    [switch]$List,
    [switch]$NoAscii
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$IpcDir     = Join-Path $ProjectRoot "headless_logs\columnist_ride_along_ipc"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Get-ValidPersonas {
    $raw = & python -c @"
import sys, os
sys.path.insert(0, r'$ProjectRoot')
from services.personas import PERSONAS
for pid, p in PERSONAS.items():
    print(f'{pid}\t{p.display_name}')
"@ 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        Write-Error "Failed to load persona list from services.personas. Is the venv active?"
        exit 1
    }
    $result = [ordered]@{}
    foreach ($line in $raw -split "`n") {
        $line = $line.Trim()
        if ($line -match "^([^\t]+)\t(.+)$") {
            $result[$Matches[1]] = $Matches[2]
        }
    }
    return $result
}

function Write-AtomicJson {
    param([string]$Path, [hashtable]$Obj)
    # Set-Content -Encoding UTF8 prepends a BOM on PowerShell 5.1, which Python's
    # json.loads rejects. Use .NET WriteAllText with UTF8Encoding($false) to write
    # plain UTF-8 (no BOM) per RFC 8259.
    $tmp = "$Path.tmp"
    $json = $Obj | ConvertTo-Json -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    Move-Item -Force -Path $tmp -Destination $Path
}

function Read-JsonOrNull {
    param([string]$Path)
    try {
        if (Test-Path $Path) {
            return Get-Content -Path $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        }
    } catch {}
    return $null
}

function Touch-Heartbeat {
    $hb = Join-Path $IpcDir "sidecar_heartbeat.txt"
    try {
        [System.IO.File]::WriteAllText($hb, [string](Get-Date -Format "o"))
    } catch {}
}

function Remove-IfExists {
    param([string]$Path)
    if (Test-Path $Path) { Remove-Item -Force -Path $Path -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------------------
# --list flag
# ---------------------------------------------------------------------------
if ($List) {
    $personas = Get-ValidPersonas
    foreach ($personaId in $personas.Keys) { Write-Output $personaId }
    exit 0
}

# ---------------------------------------------------------------------------
# Persona selection
# ---------------------------------------------------------------------------
$personas = Get-ValidPersonas
$personaIds = @($personas.Keys)

if ($PersonaArg -eq "") {
    Write-Host ""
    Write-Host "  Columnist Ride-Along v2 — choose a persona"
    Write-Host ""
    for ($i = 0; $i -lt $personaIds.Count; $i++) {
        $personaId = $personaIds[$i]
        Write-Host ("  [{0,2}]  {1,-20}  {2}" -f ($i + 1), $personaId, $personas[$personaId])
    }
    Write-Host ""
    $sel = Read-Host "  Enter number or persona id"
    $sel = $sel.Trim()
    if ($sel -match '^\d+$') {
        $idx = [int]$sel - 1
        if ($idx -lt 0 -or $idx -ge $personaIds.Count) {
            Write-Error "Invalid selection."
            exit 1
        }
        $ChosenId = $personaIds[$idx]
    } else {
        $ChosenId = $sel
    }
} else {
    $ChosenId = $PersonaArg.Trim()
}

if (-not $personas.Contains($ChosenId)) {
    Write-Error "Unknown persona '$ChosenId'. Valid ids:`n  $($personaIds -join ', ')"
    exit 1
}
$ChosenDisplay = $personas[$ChosenId]

# ---------------------------------------------------------------------------
# Ensure IPC dir exists
# ---------------------------------------------------------------------------
if (-not (Test-Path $IpcDir)) {
    New-Item -ItemType Directory -Path $IpcDir -Force | Out-Null
}

# ---------------------------------------------------------------------------
# Check bot is running (state.json must exist and be recent)
# ---------------------------------------------------------------------------
$statePath = Join-Path $IpcDir "state.json"
$state = Read-JsonOrNull $statePath
if ($null -eq $state) {
    Write-Warning "state.json not found in $IpcDir — is the bot running?"
    exit 1
}
$lastUpdate = [datetime]::Parse($state.last_update_ts)
$ageSec = ([datetime]::UtcNow - $lastUpdate.ToUniversalTime()).TotalSeconds
if ($ageSec -gt 60) {
    Write-Warning ("state.json is {0:F0}s old — bot may not be running." -f $ageSec)
    exit 1
}

# ---------------------------------------------------------------------------
# Write start.cmd
# ---------------------------------------------------------------------------
$MySidecardPid = $PID
Write-AtomicJson -Path (Join-Path $IpcDir "start.cmd") -Obj @{
    persona_id   = $ChosenId
    sidecar_pid  = $MySidecardPid
    requested_at = (Get-Date -Format "o")
}

# ---------------------------------------------------------------------------
# Poll state.json until attached / rejected / timeout
# ---------------------------------------------------------------------------
$deadline = [datetime]::UtcNow.AddSeconds(30)
$attached = $false
while ([datetime]::UtcNow -lt $deadline) {
    Touch-Heartbeat
    Start-Sleep -Milliseconds 300
    $state = Read-JsonOrNull $statePath
    if ($null -eq $state) { continue }
    if ($state.status -eq "attached" -and $state.persona_id -eq $ChosenId) {
        $attached = $true
        break
    }
    if ($state.status -eq "rejected") {
        Write-Error ("Bot rejected attach: {0}" -f $state.reason)
        exit 1
    }
}
if (-not $attached) {
    Write-Error "Timed out waiting for bot to confirm attach."
    exit 1
}

$logPath = $state.log_path
$ORIGINAL_BOT_PID = $state.bot_pid
Write-Host ""
Write-Host ("  attached -> {0} | log: {1}" -f $ChosenId, $logPath)
Write-Host "  Waiting for $ChosenDisplay posts... (Ctrl+C to quit cleanly)"
Write-Host ""

# ---------------------------------------------------------------------------
# Ctrl+C handler — write stop.cmd before exiting
# ---------------------------------------------------------------------------
$global:QuittingCleanly = $false
[Console]::TreatControlCAsInput = $false
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    if (-not $global:QuittingCleanly) {
        $stopPath = Join-Path $IpcDir "stop.cmd"
        Write-AtomicJson -Path $stopPath -Obj @{
            sidecar_pid = $MySidecardPid
            reason      = "sidecar_ctrl_c"
        }
    }
}

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
$pendingTag  = $null
$pendingSev  = $null
$pauseCount  = 0
$SpinChars   = '|', '/', '-', '\'
$spinIdx     = 0

$pausePath   = Join-Path $IpcDir "pause.json"
$feedbackPath = Join-Path $IpcDir "feedback.json"
$stopPath    = Join-Path $IpcDir "stop.cmd"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
try {
    while ($true) {
        Touch-Heartbeat

        # --- Check bot state freshness ---
        $state = Read-JsonOrNull $statePath
        if ($null -ne $state) {
            $lu = [datetime]::Parse($state.last_update_ts)
            $age = ([datetime]::UtcNow - $lu.ToUniversalTime()).TotalSeconds
            if ($age -gt 60) {
                Write-Host "`n  Bot appears unresponsive (state.json is $([int]$age)s old) — exiting."
                break
            }
            if ($state.bot_pid -ne $ORIGINAL_BOT_PID -or $state.status -eq "detached") {
                # Bot restarted (new bot_pid) or detached us.
                if ($state.bot_pid -ne $ORIGINAL_BOT_PID) {
                    Write-Host "`n  Bot restarted — detaching."
                } else {
                    Write-Host "`n  Bot detached — exiting."
                }
                break
            }
        }

        # --- Check for pause.json ---
        if (Test-Path $pausePath) {
            $pause = Read-JsonOrNull $pausePath
            if ($null -eq $pause) {
                # In-flight write; skip this cycle
                Start-Sleep -Milliseconds 300
                continue
            }
            # Sidecar consumes pause.json
            Remove-IfExists $pausePath
            $pauseCount++

            $articleId = $pause.article_id
            $headline  = $pause.headline
            $body      = $pause.body_preview
            $personaDN = $pause.persona_display_name

            if (-not $NoAscii) {
                $sep = "=" * 60
                Write-Host ""
                Write-Host $sep
                Write-Host ("  COLUMNIST RIDE-ALONG — pause #{0}" -f $pauseCount)
                Write-Host ("  Persona : {0}" -f $personaDN)
                Write-Host ("  Headline: {0}" -f $headline)
                if ($body) {
                    $preview = if ($body.Length -gt 400) { $body.Substring(0, 400) } else { $body }
                    Write-Host ("  Body    : {0}" -f $preview)
                }
                Write-Host $sep
                Write-Host "  Type feedback and Enter -- or :skip / :tag <cat> / :sev 0-3 / :quit"
            } else {
                Write-Host ("[pause #{0}] {1} | {2}" -f $pauseCount, $personaDN, $headline)
            }

            # Read input
            $line = Read-Host "feedback"
            $line = $line.Trim()

            # Parse commands
            if ($line -ieq ":quit") {
                # Write feedback record first so bot has a record, then stop.cmd
                $fbPayload = @{
                    article_id    = $articleId
                    user_feedback = $null
                    tag           = $null
                    severity      = $null
                    action        = "quit"
                }
                Write-AtomicJson -Path $feedbackPath -Obj $fbPayload

                # Brief pause for bot to consume feedback before stop.cmd lands.
                Start-Sleep -Milliseconds 400

                $global:QuittingCleanly = $true
                Write-AtomicJson -Path $stopPath -Obj @{
                    sidecar_pid = $MySidecardPid
                    reason      = "user_quit"
                }
                break
            }

            if ($line -imatch '^:tag\s+(.+)$') {
                $pendingTag = $Matches[1].Trim()
                Write-Host "   [tag set: $pendingTag]"
                # Put the pause back so user can answer it
                Write-AtomicJson -Path $pausePath -Obj $pause
                continue
            }

            if ($line -imatch '^:sev\s+(\d)$') {
                $sev = [int]$Matches[1]
                if ($sev -ge 0 -and $sev -le 3) {
                    $pendingSev = $sev
                    Write-Host "   [severity set: $pendingSev]"
                } else {
                    Write-Host "   [severity must be 0-3]"
                }
                Write-AtomicJson -Path $pausePath -Obj $pause
                continue
            }

            $action   = if ($line -eq "" -or $line -ieq ":skip") { "skip" } else { "feedback" }
            $fbText   = if ($action -eq "skip") { $null } else { $line }

            $fbPayload = @{
                article_id    = $articleId
                user_feedback = $fbText
                tag           = $pendingTag
                severity      = $pendingSev
                action        = $action
            }
            Write-AtomicJson -Path $feedbackPath -Obj $fbPayload

            $label = if ($fbText) { "`"$fbText`"" } else { "(skipped)" }
            $tagStr = if ($pendingTag) { "  tag=$pendingTag" } else { "" }
            $sevStr = if ($null -ne $pendingSev) { "  sev=$pendingSev" } else { "" }
            Write-Host ("   >> LOGGED {0}{1}{2}" -f $label, $tagStr, $sevStr)

            # Reset per-pause accumulators after a substantive submission.
            $pendingTag = $null
            $pendingSev = $null

            # Wait for bot to acknowledge (status flips back to attached).
            $ackDeadline = [datetime]::UtcNow.AddSeconds(10)
            while ([datetime]::UtcNow -lt $ackDeadline) {
                Touch-Heartbeat
                Start-Sleep -Milliseconds 300
                $s = Read-JsonOrNull $statePath
                if ($null -ne $s -and $s.status -ne "paused") { break }
            }

            Write-Host ("  Waiting for {0} posts..." -f $ChosenDisplay)

        } else {
            # No pause — print spinner
            $spin = $SpinChars[$spinIdx % $SpinChars.Count]
            $spinIdx++
            Write-Host -NoNewline ("`r  {0}  waiting for {1} posts...   " -f $spin, $ChosenDisplay)
            Start-Sleep -Milliseconds 300
        }
    }
} finally {
    # Always touch heartbeat one last time and print summary.
    if ($global:QuittingCleanly) {
        Write-Host ""
        Write-Host ""
        Write-Host "  Fetching session summary..."
        try {
            & python -c @"
import sys, os
sys.path.insert(0, r'$ProjectRoot')
from services.columnist_ride_along import summarize_session
summarize_session(r'$logPath')
"@
        } catch {
            Write-Warning "Could not print summary: $_"
        }
    }
}
