#!/usr/bin/env bash
# Columnist ride-along launcher (bash / Git Bash).
#
# Usage:
#   ./scripts/columnist_ride_along.sh                        # interactive persona menu
#   ./scripts/columnist_ride_along.sh marcus_cole            # skip menu
#   ./scripts/columnist_ride_along.sh --list                 # list valid persona ids and exit
#   ./scripts/columnist_ride_along.sh --bootstrap-existing <league_id>
#
# Options:
#   --list                         Print valid persona ids one per line and exit.
#   --bootstrap-new                Create a fresh league (default).
#   --bootstrap-existing <id>      Reuse an existing league (must be REGULAR_SEASON_ACTIVE).
#   --sim-batch-size <N>           Batch size for sim runs (default: 20, informational).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
# Fallback: on native Linux/macOS the binary is at .venv/bin/python
if [ ! -f "$PYTHON" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
fi

# ── Parse arguments ───────────────────────────────────────────────────────────
PERSONA_ID=""
DO_LIST=0
BOOTSTRAP_EXISTING=""
# Bootstrap new is default

while [[ $# -gt 0 ]]; do
    case "$1" in
        --list)
            DO_LIST=1
            shift
            ;;
        --bootstrap-new)
            shift
            ;;
        --bootstrap-existing)
            BOOTSTRAP_EXISTING="$2"
            shift 2
            ;;
        --sim-batch-size)
            SIM_BATCH_SIZE="$2"   # captured but informational only
            shift 2
            ;;
        --*)
            echo "Unknown flag: $1" >&2
            exit 1
            ;;
        *)
            if [ -z "$PERSONA_ID" ]; then
                PERSONA_ID="$1"
            fi
            shift
            ;;
    esac
done

# ── Guard: conflicting env vars ───────────────────────────────────────────────
if [ "${DBA_RIDE_ALONG:-}" = "1" ]; then
    echo "ERROR: DBA_RIDE_ALONG=1 is set. Cannot run columnist ride-along simultaneously with trade ride-along (stdin collision). Unset DBA_RIDE_ALONG first." >&2
    exit 1
fi

if [ "${DBA_HEADLESS_MODE:-}" = "1" ]; then
    echo "ERROR: DBA_HEADLESS_MODE=1 is set. Columnist ride-along requires a real Discord bot. Unset DBA_HEADLESS_MODE first." >&2
    exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set. Columnist articles cannot be generated without it." >&2
    exit 1
fi

# ── Load PERSONAS ─────────────────────────────────────────────────────────────
PERSONA_LINES=$("$PYTHON" -c "
import sys, pathlib
sys.path.insert(0, str(pathlib.Path('$PROJECT_ROOT')))
from dotenv import load_dotenv; load_dotenv(pathlib.Path('$PROJECT_ROOT') / '.env')
from services.personas import PERSONAS
for pid, p in sorted(PERSONAS.items()):
    print(f'{pid}|{p.display_name}')
" 2>/dev/null)

if [ -z "$PERSONA_LINES" ]; then
    echo "ERROR: Failed to load PERSONAS from services/personas/__init__.py." >&2
    exit 1
fi

# ── --list ────────────────────────────────────────────────────────────────────
if [ "$DO_LIST" -eq 1 ]; then
    echo "$PERSONA_LINES" | cut -d'|' -f1
    exit 0
fi

# Build arrays
declare -a PERSONA_IDS=()
declare -a PERSONA_NAMES=()
while IFS='|' read -r pid pname; do
    PERSONA_IDS+=("$pid")
    PERSONA_NAMES+=("$pname")
done <<< "$PERSONA_LINES"

# ── Resolve persona ───────────────────────────────────────────────────────────
if [ -z "$PERSONA_ID" ]; then
    echo ""
    echo "Columnist Ride-Along — pick a persona:"
    echo ""
    for i in "${!PERSONA_IDS[@]}"; do
        printf "  %2d. %-22s %s\n" "$((i + 1))" "${PERSONA_IDS[$i]}" "${PERSONA_NAMES[$i]}"
    done
    echo ""
    read -rp "Enter number or persona id: " CHOICE
    CHOICE="${CHOICE// /}"

    # Numeric?
    if [[ "$CHOICE" =~ ^[0-9]+$ ]]; then
        IDX=$((CHOICE - 1))
        if [ "$IDX" -ge 0 ] && [ "$IDX" -lt "${#PERSONA_IDS[@]}" ]; then
            PERSONA_ID="${PERSONA_IDS[$IDX]}"
        else
            echo "ERROR: $CHOICE is out of range (1..${#PERSONA_IDS[@]})." >&2
            exit 1
        fi
    else
        PERSONA_ID="$CHOICE"
    fi
fi

# Validate
VALID=0
for pid in "${PERSONA_IDS[@]}"; do
    if [ "$pid" = "$PERSONA_ID" ]; then
        VALID=1
        break
    fi
done
if [ "$VALID" -eq 0 ]; then
    echo "ERROR: '$PERSONA_ID' is not a valid persona id." >&2
    echo "Valid ids:" >&2
    for pid in "${PERSONA_IDS[@]}"; do echo "  $pid" >&2; done
    exit 1
fi

echo ""
echo "[columnist_ride_along] Target persona: $PERSONA_ID"
echo ""

# ── Bootstrap ─────────────────────────────────────────────────────────────────
if [ -n "$BOOTSTRAP_EXISTING" ]; then
    echo "[columnist_ride_along] Reusing league id=$BOOTSTRAP_EXISTING..."
    BOOTSTRAP_OUT=$("$PYTHON" "$PROJECT_ROOT/scripts/_columnist_ra_bootstrap.py" --reuse-league "$BOOTSTRAP_EXISTING" 2>&1)
    BOOTSTRAP_EXIT=$?
else
    echo "[columnist_ride_along] Creating fresh league (this may take ~30s)..."
    BOOTSTRAP_OUT=$("$PYTHON" "$PROJECT_ROOT/scripts/_columnist_ra_bootstrap.py" --new 2>&1)
    BOOTSTRAP_EXIT=$?
fi

echo "$BOOTSTRAP_OUT"

if [ "$BOOTSTRAP_EXIT" -ne 0 ]; then
    echo "ERROR: Bootstrap failed (exit $BOOTSTRAP_EXIT)." >&2
    exit 1
fi

LEAGUE_ID=$(echo "$BOOTSTRAP_OUT" | grep '^LEAGUE_ID=' | head -1 | cut -d'=' -f2)
if [ -z "$LEAGUE_ID" ]; then
    echo "ERROR: Bootstrap did not print a LEAGUE_ID." >&2
    exit 1
fi

echo "[columnist_ride_along] League id: $LEAGUE_ID"
echo "[columnist_ride_along] Starting bot (run.py)..."
echo "[columnist_ride_along] Type feedback at the pause prompt."
echo "[columnist_ride_along] Type :quit to stop cleanly."
echo ""

# ── Launch bot ────────────────────────────────────────────────────────────────
# Generate log path ONCE here so the bot process and the post-exit summary
# subprocess both see the same DBA_COLUMNIST_RA_LOG value.  Without this,
# each process imports the module at a different clock instant and gets a
# different _SESSION_TS, causing the summary to look for a file that doesn't exist.
SESSION_TS="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="$PROJECT_ROOT/headless_logs"
mkdir -p "$LOG_DIR"
export DBA_COLUMNIST_RA_LOG="$LOG_DIR/columnist_ride_along_${PERSONA_ID}_${SESSION_TS}.jsonl"
export DBA_COLUMNIST_RIDE_ALONG="$PERSONA_ID"

echo "[columnist_ride_along] Log file: $DBA_COLUMNIST_RA_LOG"

cd "$PROJECT_ROOT"
"$PYTHON" "$PROJECT_ROOT/run.py"

# ── Post-exit summary ─────────────────────────────────────────────────────────
echo ""
echo "[columnist_ride_along] Bot exited. Printing session summary..."
# DBA_COLUMNIST_RA_LOG is still exported — the summary subprocess inherits it
# and reads the exact same file the bot wrote.
"$PYTHON" -c "
import sys, pathlib
sys.path.insert(0, '$PROJECT_ROOT')
from dotenv import load_dotenv; load_dotenv(pathlib.Path('$PROJECT_ROOT') / '.env')
import os
os.environ['DBA_COLUMNIST_RIDE_ALONG'] = '$PERSONA_ID'
from services.columnist_ride_along import summarize_session
summarize_session()
"
