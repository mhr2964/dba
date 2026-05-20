"""Watchdog wrapper that respawns the bot when /admin restart requests it.

Launch the bot via `python run.py` instead of `python main.py`.

`/admin restart` writes exit code 42 via sys.exit(42) and shuts the client
down. This wrapper sees the 42 and immediately re-spawns main.py. Any other
exit code (0 = clean shutdown, anything else = crash) terminates the loop.
"""
import os
import subprocess
import sys

RESTART_EXIT_CODE = 42

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "main.py")


def run() -> int:
    while True:
        print(f"[watchdog] starting bot ({sys.executable} {MAIN})", flush=True)
        result = subprocess.run([sys.executable, MAIN], cwd=HERE)
        if result.returncode == RESTART_EXIT_CODE:
            print("[watchdog] restart requested — respawning", flush=True)
            continue
        print(f"[watchdog] bot exited with code {result.returncode} — stopping", flush=True)
        return result.returncode


if __name__ == "__main__":
    sys.exit(run())
