"""0015_incident_offer.py

Adds ``offer`` (boolean, default false) to ``incident``.

D8 sends ``"offer": true`` in the page request of every lead it offers to one
person. Those incidents are answered with Accept and Reject in D8-Chat (when
``D8_LEAD_RESPONSE_URL`` is set); D8's other lead pages ("wrote in again", a
failed intake) and every older incident stay false and keep Acknowledge.

Fresh DBs initialized via ``init_db.py`` already have the column because the
``Incident`` model was updated in lockstep; ``IF NOT EXISTS`` makes this
migration a no-op there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Add the offer column; existing incidents get false."""
    db.execute_sql(
        "ALTER TABLE incident ADD COLUMN IF NOT EXISTS offer BOOLEAN NOT NULL "
        "DEFAULT FALSE"
    )


def rollback():
    """Drop the offer column."""
    db.execute_sql("ALTER TABLE incident DROP COLUMN IF EXISTS offer")
