import logging
import sys
from pathlib import Path

# bot.log is the canonical live log for tests/tooling that tail file output.
# Without a FileHandler the stdout-only setup left bot.log as a stale artifact
# from old shell redirects, which made `tail bot.log` mid-test look like the
# bot was silent. Keep both handlers in sync so stdout (background-task capture)
# and bot.log agree.
_LOG_PATH = Path(__file__).resolve().parent.parent / "bot.log"


def setup_logging() -> None:
    file_handler = logging.FileHandler(_LOG_PATH, mode="a", encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), file_handler],
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
