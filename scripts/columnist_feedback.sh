#!/usr/bin/env bash
# Columnist ride-along v2 sidecar CLI (bash).
#
# Usage:
#   bash ./scripts/columnist_feedback.sh [persona_id] [--list] [--no-ascii]
#
# Attaches to a running DBA bot via file IPC in
# headless_logs/columnist_ride_along_ipc/ and pauses the sim each time the
# chosen persona posts an article, prompting for free-form feedback.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
IPC_DIR="$PROJECT_ROOT/headless_logs/columnist_ride_along_ipc"

PERSONA_ARG=""
NO_ASCII=0
LIST=0

# Parse args
for arg in "$@"; do
    case "$arg" in
        --list)     LIST=1 ;;
        --no-ascii) NO_ASCII=1 ;;
        -*)         echo "Unknown flag: $arg" >&2; exit 1 ;;
        *)          PERSONA_ARG="$arg" ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

get_valid_personas() {
    python -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT')
from services.personas import PERSONAS
for pid, p in PERSONAS.items():
    print(f'{pid}\t{p.display_name}')
" 2>/dev/null
}

atomic_write_json() {
    local path="$1"
    local content="$2"
    local tmp="${path}.tmp"
    printf '%s' "$content" > "$tmp"
    mv -f "$tmp" "$path"
}

read_json_field() {
    # read_json_field <file> <field>
    python -c "
import sys, json
try:
    d = json.load(open('$1', encoding='utf-8'))
    print(d.get('$2', ''))
except Exception:
    pass
" 2>/dev/null || true
}

read_state_field() {
    read_json_field "$IPC_DIR/state.json" "$1"
}

touch_heartbeat() {
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$IPC_DIR/sidecar_heartbeat.txt" 2>/dev/null || true
}

remove_if_exists() {
    [ -f "$1" ] && rm -f "$1" || true
}

ts_now_utc() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

make_json_feedback() {
    # make_json_feedback <article_id> <feedback_or_null> <tag_or_null> <sev_or_null> <action>
    python -c "
import json, sys
a, fb, tag, sev, action = sys.argv[1:]
d = {
    'article_id': a,
    'user_feedback': None if fb == '__null__' else fb,
    'tag': None if tag == '__null__' else tag,
    'severity': None if sev == '__null__' else int(sev),
    'action': action,
}
print(json.dumps(d))
" "$1" "$2" "$3" "$4" "$5" 2>/dev/null
}

make_json() {
    # Minimal inline JSON builder for simple flat dicts.
    python -c "
import json, sys
args = sys.argv[1:]
d = {}
for i in range(0, len(args), 2):
    k = args[i]; v = args[i+1]
    if v == '__null__': v = None
    elif v.isdigit(): v = int(v)
    d[k] = v
print(json.dumps(d))
" "$@" 2>/dev/null
}

# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------
if [ "$LIST" -eq 1 ]; then
    get_valid_personas | cut -f1
    exit 0
fi

# ---------------------------------------------------------------------------
# Load persona list
# ---------------------------------------------------------------------------
PERSONA_LIST="$(get_valid_personas)"
if [ -z "$PERSONA_LIST" ]; then
    echo "[error] Failed to load persona list. Is the venv active?" >&2
    exit 1
fi

PERSONA_IDS=()
PERSONA_DISPLAY=()
while IFS=$'\t' read -r pid dn; do
    PERSONA_IDS+=("$pid")
    PERSONA_DISPLAY+=("$dn")
done <<< "$PERSONA_LIST"

# ---------------------------------------------------------------------------
# Persona selection
# ---------------------------------------------------------------------------
if [ -z "$PERSONA_ARG" ]; then
    echo ""
    echo "  Columnist Ride-Along v2 — choose a persona"
    echo ""
    for i in "${!PERSONA_IDS[@]}"; do
        printf "  [%2d]  %-20s  %s\n" $((i+1)) "${PERSONA_IDS[$i]}" "${PERSONA_DISPLAY[$i]}"
    done
    echo ""
    read -r -p "  Enter number or persona id: " SEL
    SEL="${SEL// /}"
    if [[ "$SEL" =~ ^[0-9]+$ ]]; then
        IDX=$((SEL - 1))
        if [ "$IDX" -lt 0 ] || [ "$IDX" -ge "${#PERSONA_IDS[@]}" ]; then
            echo "[error] Invalid selection." >&2; exit 1
        fi
        CHOSEN_ID="${PERSONA_IDS[$IDX]}"
    else
        CHOSEN_ID="$SEL"
    fi
else
    CHOSEN_ID="$PERSONA_ARG"
fi

# Validate chosen id
CHOSEN_DISPLAY=""
for i in "${!PERSONA_IDS[@]}"; do
    if [ "${PERSONA_IDS[$i]}" = "$CHOSEN_ID" ]; then
        CHOSEN_DISPLAY="${PERSONA_DISPLAY[$i]}"
        break
    fi
done
if [ -z "$CHOSEN_DISPLAY" ]; then
    echo "[error] Unknown persona '$CHOSEN_ID'. Valid ids: ${PERSONA_IDS[*]}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure IPC dir exists
# ---------------------------------------------------------------------------
mkdir -p "$IPC_DIR"

# ---------------------------------------------------------------------------
# Check bot state freshness
# ---------------------------------------------------------------------------
STATE_PATH="$IPC_DIR/state.json"
if [ ! -f "$STATE_PATH" ]; then
    echo "[error] state.json not found — is the bot running?" >&2
    exit 1
fi
LAST_UPDATE="$(read_json_field "$STATE_PATH" "last_update_ts")"
AGE_SEC="$(python -c "
from datetime import datetime, timezone
lu = datetime.fromisoformat('$LAST_UPDATE'.replace('Z', '+00:00'))
age = (datetime.now(timezone.utc) - lu).total_seconds()
print(int(age))
" 2>/dev/null || echo 9999)"
if [ "$AGE_SEC" -gt 60 ]; then
    echo "[warning] state.json is ${AGE_SEC}s old — bot may not be running." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Write start.cmd
# ---------------------------------------------------------------------------
SIDECAR_PID="$$"
atomic_write_json "$IPC_DIR/start.cmd" \
    "$(make_json persona_id "$CHOSEN_ID" sidecar_pid "$SIDECAR_PID" requested_at "$(ts_now_utc)")"

# ---------------------------------------------------------------------------
# Poll for attached / rejected
# ---------------------------------------------------------------------------
DEADLINE=$(($(date +%s) + 30))
ATTACHED=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    touch_heartbeat
    sleep 0.3
    STATUS="$(read_state_field "status")"
    PERSONA_BACK="$(read_state_field "persona_id")"
    if [ "$STATUS" = "attached" ] && [ "$PERSONA_BACK" = "$CHOSEN_ID" ]; then
        ATTACHED=1
        break
    fi
    if [ "$STATUS" = "rejected" ]; then
        REASON="$(read_state_field "reason")"
        echo "[error] Bot rejected attach: $REASON" >&2
        exit 1
    fi
done
if [ "$ATTACHED" -eq 0 ]; then
    echo "[error] Timed out waiting for bot to confirm attach." >&2
    exit 1
fi

LOG_PATH="$(read_state_field "log_path")"
echo ""
echo "  attached -> $CHOSEN_ID | log: $LOG_PATH"
echo "  Waiting for $CHOSEN_DISPLAY posts... (Ctrl+C to quit cleanly)"
echo ""

# ---------------------------------------------------------------------------
# Ctrl+C / EXIT handler
# ---------------------------------------------------------------------------
QUITTING_CLEANLY=0
cleanup() {
    if [ "$QUITTING_CLEANLY" -eq 0 ]; then
        atomic_write_json "$IPC_DIR/stop.cmd" \
            "$(make_json sidecar_pid "$SIDECAR_PID" reason "sidecar_ctrl_c")" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
PENDING_TAG="__null__"
PENDING_SEV="__null__"
PAUSE_COUNT=0
SPIN_CHARS=('|' '/' '-' '\')
SPIN_IDX=0

PAUSE_PATH="$IPC_DIR/pause.json"
FEEDBACK_PATH="$IPC_DIR/feedback.json"
STOP_PATH="$IPC_DIR/stop.cmd"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while true; do
    touch_heartbeat

    # Check bot state freshness
    LAST_UPDATE="$(read_state_field "last_update_ts" 2>/dev/null || echo "")"
    if [ -n "$LAST_UPDATE" ]; then
        AGE="$(python -c "
from datetime import datetime, timezone
try:
    lu = datetime.fromisoformat('$LAST_UPDATE'.replace('Z', '+00:00'))
    print(int((datetime.now(timezone.utc) - lu).total_seconds()))
except Exception:
    print(0)
" 2>/dev/null || echo 0)"
        if [ "$AGE" -gt 60 ]; then
            printf "\r\n  Bot appears unresponsive (state.json is ${AGE}s old) — exiting.\n"
            break
        fi
    fi

    # Check for pause.json
    if [ -f "$PAUSE_PATH" ]; then
        # Read and consume
        PAUSE_JSON="$(cat "$PAUSE_PATH" 2>/dev/null || echo "")"
        if [ -z "$PAUSE_JSON" ]; then
            sleep 0.3
            continue
        fi
        remove_if_exists "$PAUSE_PATH"
        PAUSE_COUNT=$((PAUSE_COUNT + 1))

        ARTICLE_ID="$(echo "$PAUSE_JSON" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('article_id',''))")"
        HEADLINE="$(echo "$PAUSE_JSON"   | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('headline',''))")"
        BODY_PRE="$(echo "$PAUSE_JSON"   | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('body_preview','')[:400])")"
        PERSONA_DN="$(echo "$PAUSE_JSON" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('persona_display_name',''))")"

        printf "\r\n"
        if [ "$NO_ASCII" -eq 0 ]; then
            SEP="$(printf '=%.0s' {1..60})"
            echo "$SEP"
            printf "  COLUMNIST RIDE-ALONG — pause #%d\n" "$PAUSE_COUNT"
            printf "  Persona : %s\n" "$PERSONA_DN"
            printf "  Headline: %s\n" "$HEADLINE"
            if [ -n "$BODY_PRE" ]; then
                printf "  Body    : %s\n" "$BODY_PRE"
            fi
            echo "$SEP"
            echo "  Type feedback and Enter -- or :skip / :tag <cat> / :sev 0-3 / :quit"
        else
            printf "[pause #%d] %s | %s\n" "$PAUSE_COUNT" "$PERSONA_DN" "$HEADLINE"
        fi

        read -r -p "feedback> " LINE || LINE=":quit"
        LINE="${LINE#"${LINE%%[![:space:]]*}"}"  # ltrim

        # Command dispatch
        case "$LINE" in
            :quit)
                # Write quit feedback record, then stop.cmd
                FB="$(make_json_feedback "$ARTICLE_ID" "__null__" "__null__" "__null__" "quit")"
                atomic_write_json "$FEEDBACK_PATH" "$FB"
                sleep 0.4
                QUITTING_CLEANLY=1
                atomic_write_json "$STOP_PATH" \
                    "$(make_json sidecar_pid "$SIDECAR_PID" reason "user_quit")"
                break
                ;;
            :tag\ *)
                TAG="${LINE#:tag }"
                TAG="${TAG// /}"
                PENDING_TAG="$TAG"
                echo "   [tag set: $PENDING_TAG]"
                # Restore pause.json so user can answer the current pause
                atomic_write_json "$PAUSE_PATH" "$PAUSE_JSON"
                continue
                ;;
            :sev\ [0-3])
                PENDING_SEV="${LINE#:sev }"
                echo "   [severity set: $PENDING_SEV]"
                atomic_write_json "$PAUSE_PATH" "$PAUSE_JSON"
                continue
                ;;
            :sev\ *)
                echo "   [severity must be 0-3]"
                atomic_write_json "$PAUSE_PATH" "$PAUSE_JSON"
                continue
                ;;
            :skip|"")
                ACTION="skip"
                FB_TEXT="__null__"
                ;;
            *)
                ACTION="feedback"
                FB_TEXT="$LINE"
                ;;
        esac

        FB="$(make_json_feedback "$ARTICLE_ID" "$FB_TEXT" "$PENDING_TAG" "$PENDING_SEV" "$ACTION")"
        atomic_write_json "$FEEDBACK_PATH" "$FB"

        LABEL="$([ "$FB_TEXT" = "__null__" ] && echo "(skipped)" || echo "\"$FB_TEXT\"")"
        TAG_STR="$([ "$PENDING_TAG" != "__null__" ] && echo "  tag=$PENDING_TAG" || echo "")"
        SEV_STR="$([ "$PENDING_SEV" != "__null__" ] && echo "  sev=$PENDING_SEV" || echo "")"
        echo "   >> LOGGED ${LABEL}${TAG_STR}${SEV_STR}"

        PENDING_TAG="__null__"
        PENDING_SEV="__null__"

        # Wait for bot acknowledgement (status leaves paused)
        ACK_DEADLINE=$(($(date +%s) + 10))
        while [ "$(date +%s)" -lt "$ACK_DEADLINE" ]; do
            touch_heartbeat
            sleep 0.3
            S="$(read_state_field "status" 2>/dev/null || echo "paused")"
            if [ "$S" != "paused" ]; then break; fi
        done

        echo "  Waiting for $CHOSEN_DISPLAY posts..."

    else
        # Spinner
        SPIN="${SPIN_CHARS[$((SPIN_IDX % 4))]}"
        SPIN_IDX=$((SPIN_IDX + 1))
        printf "\r  %s  waiting for %s posts...   " "$SPIN" "$CHOSEN_DISPLAY"
        sleep 0.3
    fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo ""
if [ "$QUITTING_CLEANLY" -eq 1 ] && [ -n "$LOG_PATH" ]; then
    echo "  Fetching session summary..."
    python -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT')
from services.columnist_ride_along import summarize_session
summarize_session('$LOG_PATH')
" 2>/dev/null || echo "  (could not load summary)"
fi
