import asyncio
import os
import sys
import time
import pathlib

from bot.client import DBABot
from core.config import config
from core.logging import setup_logging

_LOCK = pathlib.Path(__file__).parent / ".bot.lock"


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _acquire_lock() -> None:
    """Refuse to start if another instance is already running.

    During a restart the old process holds the lock until its finally clause
    fires.  Allow up to 10 seconds for the old PID to exit before giving up,
    so a /admin restart doesn't require manual lock cleanup.
    """
    deadline = time.time() + 10.0
    while True:
        if _LOCK.exists():
            try:
                existing_pid = int(_LOCK.read_text().strip())
                if _pid_running(existing_pid):
                    if time.time() < deadline:
                        time.sleep(0.5)
                        continue
                    print(
                        f"Bot already running (PID {existing_pid})."
                        f" Kill it first or delete {_LOCK}."
                    )
                    sys.exit(1)
            except ValueError:
                pass
            # Stale lock — overwrite it.
        _LOCK.write_text(str(os.getpid()))
        return


def _release_lock() -> None:
    try:
        _LOCK.unlink(missing_ok=True)
    except OSError:
        pass


async def main():
    setup_logging()
    async with DBABot() as bot:
        await bot.start(config.discord_token)


_acquire_lock()
try:
    asyncio.run(main())
finally:
    _release_lock()
