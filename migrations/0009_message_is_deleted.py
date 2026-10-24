"""0009_message_is_deleted.py

Adds the ``is_deleted`` boolean to the ``message`` table.

Deleting a message that starts a thread used to delete the row, which set its
replies' parent to NULL: they vanished from the thread panel and from the
timeline (they're thread replies), with no way back to them. Now the parent
stays as a "This message was deleted." placeholder, its content, attachments,
reactions and poll removed. Messages without replies are still deleted
outright.

Fresh DBs initialized via ``init_db.py`` already have the column because the
``Message`` model was updated in lockstep; ``IF NOT EXISTS`` makes this a no-op
there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Add the is_deleted column, defaulting existing rows to FALSE."""
    db.execute_sql(
        "ALTER TABLE message ADD COLUMN IF NOT EXISTS is_deleted "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )


def rollback():
    """Drop the is_deleted column."""
    db.execute_sql("ALTER TABLE message DROP COLUMN IF EXISTS is_deleted")
