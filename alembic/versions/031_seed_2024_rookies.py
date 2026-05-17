"""Mark 2024 NBA Draft class players as is_rookie=TRUE for the 2024-25 season.

ROY award eligibility requires is_rookie=TRUE, but the initial reseed did not
set this flag because the seeding code defaulted is_rookie to FALSE for all
players.  This migration finds the known 2024 draft class by name and marks
them correctly.

The name list uses the display names as stored by the BallDontLie seed; accent
characters are preserved and compared case-insensitively via ILIKE / LOWER().

Revision ID: 031
Revises: 030
Create Date: 2026-05-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Known 2024 NBA Draft class — first_name + ' ' + last_name as stored in DB.
# UNACCENT() is not guaranteed available; use explicit name variants where needed.
_DRAFT_CLASS_SQL = """
UPDATE players
SET is_rookie = TRUE
WHERE (
    (lower(first_name) = lower(fn) AND lower(last_name) = lower(ln))
)
FROM (VALUES
    ('Zaccharie', 'Risacher'),
    ('Alex', 'Sarr'),
    ('Alexandre', 'Sarr'),
    ('Reed', 'Sheppard'),
    ('Stephon', 'Castle'),
    ('Rob', 'Dillingham'),
    ('Tidjane', 'Salaun'),
    ('Donovan', 'Clingan'),
    ('Carlton', 'Carrington'),
    ('Nikola', 'Topic'),
    ('Cody', 'Williams'),
    ('Matas', 'Buzelis'),
    ('Dalton', 'Knecht'),
    ('Jared', 'McCain'),
    ('Baylor', 'Scheierman'),
    ('Devin', 'Carter'),
    ('Ryan', 'Dunn'),
    ('Isaiah', 'Collier'),
    ('Jaylen', 'Wells'),
    ('Dillon', 'Jones'),
    ('Jonathan', 'Mogbo'),
    ('Kyle', 'Filipowski'),
    ('Yves', 'Missi'),
    ('KyShawn', 'George'),
    ('Jamal', 'Shead'),
    ('Oso', 'Ighodaro'),
    ('Ronald', 'Holland'),
    ('Tristan', 'Da Silva'),
    ('Johnny', 'Furphy'),
    ('Jaylon', 'Tyson'),
    ('Pelle', 'Larsson'),
    ('Enrique', 'Freeman'),
    ('Ajay', 'Mitchell'),
    ('Cam', 'Christie'),
    ('Quinten', 'Post'),
    ('Adem', 'Bona'),
    ('Tyler', 'Kolek'),
    ('Ariel', 'Hukporti'),
    ('Bobi', 'Klintman'),
    ('Pacome', 'Dadiet'),
    ('Antonio', 'Reeves'),
    ('Zach', 'Edey'),
    ('Bronny', 'James'),
    ('AJ', 'Johnson'),
    ('Cam', 'Spencer'),
    ('Kel''el', 'Ware'),
    ('Ja''Kobe', 'Walter'),
    ('K.J.', 'Simpson'),
    ('Harrison', 'Ingram'),
    ('Matas', 'Buzelis')
) AS draft(fn, ln)
WHERE lower(players.first_name) = lower(draft.fn)
  AND lower(players.last_name) = lower(draft.ln)
"""


def upgrade() -> None:
    # Use a direct UPDATE with a values list so no external data is needed.
    # The query is idempotent — re-running it on an already-updated row is a no-op.
    op.execute("""
        UPDATE players
        SET is_rookie = TRUE
        WHERE (lower(first_name), lower(last_name)) IN (
            ('zaccharie', 'risacher'),
            ('alex', 'sarr'),
            ('alexandre', 'sarr'),
            ('reed', 'sheppard'),
            ('stephon', 'castle'),
            ('rob', 'dillingham'),
            ('tidjane', 'salaun'),
            ('donovan', 'clingan'),
            ('carlton', 'carrington'),
            ('nikola', 'topic'),
            ('cody', 'williams'),
            ('matas', 'buzelis'),
            ('dalton', 'knecht'),
            ('jared', 'mccain'),
            ('baylor', 'scheierman'),
            ('devin', 'carter'),
            ('ryan', 'dunn'),
            ('isaiah', 'collier'),
            ('jaylen', 'wells'),
            ('dillon', 'jones'),
            ('jonathan', 'mogbo'),
            ('kyle', 'filipowski'),
            ('yves', 'missi'),
            ('kyshawn', 'george'),
            ('jamal', 'shead'),
            ('oso', 'ighodaro'),
            ('ronald', 'holland'),
            ('tristan', 'da silva'),
            ('johnny', 'furphy'),
            ('jaylon', 'tyson'),
            ('pelle', 'larsson'),
            ('enrique', 'freeman'),
            ('ajay', 'mitchell'),
            ('cam', 'christie'),
            ('quinten', 'post'),
            ('adem', 'bona'),
            ('tyler', 'kolek'),
            ('ariel', 'hukporti'),
            ('bobi', 'klintman'),
            ('pacome', 'dadiet'),
            ('antonio', 'reeves'),
            ('zach', 'edey'),
            ('bronny', 'james'),
            ('aj', 'johnson'),
            ('cam', 'spencer'),
            ('kel''el', 'ware'),
            ('ja''kobe', 'walter'),
            ('k.j.', 'simpson'),
            ('harrison', 'ingram')
        )
    """)


def downgrade() -> None:
    # Revert: clear is_rookie for the 2024 draft class.
    # This intentionally does not clear rookies set by other means.
    op.execute("""
        UPDATE players
        SET is_rookie = FALSE
        WHERE (lower(first_name), lower(last_name)) IN (
            ('zaccharie', 'risacher'),
            ('alex', 'sarr'),
            ('alexandre', 'sarr'),
            ('reed', 'sheppard'),
            ('stephon', 'castle'),
            ('rob', 'dillingham'),
            ('tidjane', 'salaun'),
            ('donovan', 'clingan'),
            ('carlton', 'carrington'),
            ('nikola', 'topic'),
            ('cody', 'williams'),
            ('matas', 'buzelis'),
            ('dalton', 'knecht'),
            ('jared', 'mccain'),
            ('baylor', 'scheierman'),
            ('devin', 'carter'),
            ('ryan', 'dunn'),
            ('isaiah', 'collier'),
            ('jaylen', 'wells'),
            ('dillon', 'jones'),
            ('jonathan', 'mogbo'),
            ('kyle', 'filipowski'),
            ('yves', 'missi'),
            ('kyshawn', 'george'),
            ('jamal', 'shead'),
            ('oso', 'ighodaro'),
            ('ronald', 'holland'),
            ('tristan', 'da silva'),
            ('johnny', 'furphy'),
            ('jaylon', 'tyson'),
            ('pelle', 'larsson'),
            ('enrique', 'freeman'),
            ('ajay', 'mitchell'),
            ('cam', 'christie'),
            ('quinten', 'post'),
            ('adem', 'bona'),
            ('tyler', 'kolek'),
            ('ariel', 'hukporti'),
            ('bobi', 'klintman'),
            ('pacome', 'dadiet'),
            ('antonio', 'reeves'),
            ('zach', 'edey'),
            ('bronny', 'james'),
            ('aj', 'johnson'),
            ('cam', 'spencer'),
            ('kel''el', 'ware'),
            ('ja''kobe', 'walter'),
            ('k.j.', 'simpson'),
            ('harrison', 'ingram')
        )
    """)
