"""0005_add_send_on_enter.py

Adds the ``send_on_enter`` boolean to the ``user`` table.

Backs the per-user "send message with Enter vs Ctrl+Enter" preference
(Preferences menu in the profile slide-out). Defaults to TRUE (Enter sends,
Shift+Enter inserts a newline), which matches the app's prior default behavior.

Existing prod DBs need ``./smalls.py migrate`` (or ``auto migrate d8-chat``).
Fresh DBs initialized via ``init_db.py`` already have the column because the
``User`` model was updated in lockstep; the ``IF NOT EXISTS`` guard makes this
migration a no-op there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Add the send_on_enter column, defaulting existing rows to TRUE."""
    db.execute_sql(
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS send_on_enter '
        "BOOLEAN NOT NULL DEFAULT TRUE"
    )


def rollback():
    """Drop the send_on_enter column."""
    db.execute_sql('ALTER TABLE "user" DROP COLUMN IF EXISTS send_on_enter')
