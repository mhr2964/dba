"""Add ON DELETE CASCADE to player_id FKs in award_results, award_votes, game_box_scores, fa_offers.

Without CASCADE, deleting a league (which cascades to players) raises ForeignKeyViolationError
from tables that hold player_id references without a matching cascade rule.

Revision ID: 030
Revises: 029
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # award_results.player_id — missing CASCADE
    op.execute("""
        ALTER TABLE award_results
            DROP CONSTRAINT IF EXISTS award_results_player_id_fkey,
            ADD CONSTRAINT award_results_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    """)

    # award_votes.player_id — missing CASCADE
    op.execute("""
        ALTER TABLE award_votes
            DROP CONSTRAINT IF EXISTS award_votes_player_id_fkey,
            ADD CONSTRAINT award_votes_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    """)

    # game_box_scores.player_id — missing CASCADE
    # (rows are also removed via game_id CASCADE, but a direct player delete would block)
    op.execute("""
        ALTER TABLE game_box_scores
            DROP CONSTRAINT IF EXISTS game_box_scores_player_id_fkey,
            ADD CONSTRAINT game_box_scores_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    """)

    # fa_offers.player_id — missing CASCADE
    op.execute("""
        ALTER TABLE fa_offers
            DROP CONSTRAINT IF EXISTS fa_offers_player_id_fkey,
            ADD CONSTRAINT fa_offers_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    """)


def downgrade() -> None:
    # Restore original constraints without CASCADE
    op.execute("""
        ALTER TABLE award_results
            DROP CONSTRAINT IF EXISTS award_results_player_id_fkey,
            ADD CONSTRAINT award_results_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id)
    """)
    op.execute("""
        ALTER TABLE award_votes
            DROP CONSTRAINT IF EXISTS award_votes_player_id_fkey,
            ADD CONSTRAINT award_votes_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id)
    """)
    op.execute("""
        ALTER TABLE game_box_scores
            DROP CONSTRAINT IF EXISTS game_box_scores_player_id_fkey,
            ADD CONSTRAINT game_box_scores_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id)
    """)
    op.execute("""
        ALTER TABLE fa_offers
            DROP CONSTRAINT IF EXISTS fa_offers_player_id_fkey,
            ADD CONSTRAINT fa_offers_player_id_fkey
                FOREIGN KEY (player_id) REFERENCES players(id)
    """)
