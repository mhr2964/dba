import asyncio
import os
import sys
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
    """Refuse to start if another instance is already running."""
    if _LOCK.exists():
        try:
            existing_pid = int(_LOCK.read_text().strip())
            if _pid_running(existing_pid):
                print(f"Bot already running (PID {existing_pid}). Kill it first or delete {_LOCK}.")
                sys.exit(1)
        except ValueError:
            pass
        # Stale lock — overwrite it.
    _LOCK.write_text(str(os.getpid()))


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
