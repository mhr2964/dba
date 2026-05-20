#!/usr/bin/env bash
# Ride-along headless test launcher for bash / Git Bash.
# Usage from any shell, anywhere in the project:
#   ./scripts/ride_along.sh
# Optional flags pass through, e.g.:
#   ./scripts/ride_along.sh --force-trades-interval 3

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"
DBA_HEADLESS_MODE=1 ./.venv/Scripts/python.exe scripts/headless_test.py --force-trades --ride-along "$@"
