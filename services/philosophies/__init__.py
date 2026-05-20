"""Philosophy bias registry — auto-discovers every sibling module.

Adding a new philosophy:
1. Create ``services/philosophies/<name>.py`` with ``@register_philosophy("<name>")``
2. Add a new Alembic migration extending the teams_coach_philosophy_check constraint
3. The bot startup assertion will catch any drift before it reaches production
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

from core.logging import get_logger
from ._registry import REGISTRY, register_philosophy

log = get_logger(__name__)

_pkg_path = Path(__file__).parent
for _, modname, _ in pkgutil.iter_modules([str(_pkg_path)]):
    if modname.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{modname}")

# Public alias — preserves all existing ``from services.philosophies import PHILOSOPHY_BIASES`` callers.
PHILOSOPHY_BIASES: dict = REGISTRY


async def assert_philosophy_constraint_sync(conn) -> None:
    """Assert that PHILOSOPHY_BIASES keys are covered by the DB CHECK constraint.

    Call once at bot startup (after DB connection is established) to catch drift
    between PHILOSOPHY_BIASES and the teams_coach_philosophy_check constraint
    before it causes league-create failures in production.

    Raises RuntimeError if any key in PHILOSOPHY_BIASES is absent from the
    constraint's IN-list, so the problem is immediately visible in startup logs.
    """
    row = await conn.fetchrow(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.conname = 'teams_coach_philosophy_check'
          AND t.relname = 'teams'
        """
    )
    if row is None:
        log.warning(
            "philosophies: teams_coach_philosophy_check constraint not found — "
            "run migration 042 to add it"
        )
        return

    constraint_def = row["def"]

    def _in_constraint(key: str) -> bool:
        # Use word-boundary match so 'star' doesn't match 'star_maxer' substring.
        return bool(re.search(r"\b" + re.escape(key) + r"\b", constraint_def))

    missing = [k for k in PHILOSOPHY_BIASES if not _in_constraint(k)]
    if missing:
        msg = (
            f"philosophies: PHILOSOPHY_BIASES ↔ CHECK constraint drift detected. "
            f"Keys missing from constraint: {missing}. "
            f"Create a new Alembic migration to add them."
        )
        log.error(msg)
        raise RuntimeError(msg)

    # Reverse check: warn if the DB constraint mentions values not in the Python registry.
    # Don't crash — the DB may temporarily have legacy values during migration.
    # Extract quoted values from the constraint definition (e.g. 'star_maxer').
    db_values = set(re.findall(r"'([^']+)'", constraint_def))
    legacy = db_values - set(PHILOSOPHY_BIASES.keys())
    if legacy:
        log.warning(
            "philosophies: DB CHECK constraint has values not in PHILOSOPHY_BIASES "
            "(legacy migration values?): %s", sorted(legacy)
        )

    log.debug("philosophies: constraint sync OK (%d philosophies)", len(PHILOSOPHY_BIASES))


__all__ = ["PHILOSOPHY_BIASES", "register_philosophy", "assert_philosophy_constraint_sync"]
