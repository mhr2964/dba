"""Backfill ast_tendency for playmaker centers who are stuck at default 50.

Jokić averages 9+ APG in real life; center position average is ~2 APG, so
(9/2)*50 = 225 → capped to 95.  If fix_stat_calibration.py was not run for
the current league this value sits at 50, producing 2-3 APG in sim instead
of 8-9.

This migration targets players whose ast_tendency is still at the default 50
AND whose real-life passing profile would produce a high tendency value.
Rather than re-running the full BDL calibration script here, we target the
known playmaker centers by name and set them to the value the formula yields.

Revision ID: 033
Revises: 032
Create Date: 2026-05-17
"""
from typing import Sequence, Union
from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nikola Jokić: ~9.0 APG, C avg ~2.0 APG → (9.0/2.0)*50 = 225 → capped to 95.
    # Only update if still at the default 50 so re-running is safe.
    op.execute(
        """
        UPDATE players
        SET ast_tendency = 95
        WHERE LOWER(first_name) = 'nikola'
          AND LOWER(last_name) = 'jokic'
          AND ast_tendency = 50
        """
    )
    # Draymond Green: ~6.8 APG, PF avg ~2.5 APG → (6.8/2.5)*50 = 136 → capped to 95.
    op.execute(
        """
        UPDATE players
        SET ast_tendency = 95
        WHERE LOWER(first_name) = 'draymond'
          AND LOWER(last_name) = 'green'
          AND ast_tendency = 50
        """
    )
    # Bam Adebayo: ~3.3 APG, C avg ~2.0 APG → (3.3/2.0)*50 = 82.5 → 83.
    op.execute(
        """
        UPDATE players
        SET ast_tendency = 83
        WHERE LOWER(first_name) = 'bam'
          AND LOWER(last_name) = 'adebayo'
          AND ast_tendency = 50
        """
    )
    # Julius Randle: ~3.9 APG, PF avg ~2.5 APG → (3.9/2.5)*50 = 78.
    op.execute(
        """
        UPDATE players
        SET ast_tendency = 78
        WHERE LOWER(first_name) = 'julius'
          AND LOWER(last_name) = 'randle'
          AND ast_tendency = 50
        """
    )


def downgrade() -> None:
    # Cannot safely revert individual rows without knowing prior values.
    pass
