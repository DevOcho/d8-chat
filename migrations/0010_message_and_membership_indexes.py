"""0010_message_and_membership_indexes.py

Two indexes:

- ``message (conversation_id, created_at)``. Opening a conversation sorts its
  newest page by ``created_at``, and the unread and "anything new?" checks
  range over it. With only the single-column FK index, each scanned and
  sorted the whole conversation.
- A unique ``channelmember (user_id, channel_id)``. Joining was check-then-
  create, so a double click added a second membership row (duplicate badges
  and sounds, an inflated member count). Existing duplicates are removed
  first, keeping an admin row if there is one, otherwise the oldest.

Both are built ``CONCURRENTLY`` so writes aren't blocked while they build
(smalls runs migrations in autocommit mode, which that requires). Fresh DBs
from ``init_db.py`` get them from the model ``Meta``; ``IF NOT EXISTS`` makes
this a no-op there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Drop duplicate memberships, then build both indexes."""
    db.execute_sql(
        "DELETE FROM channelmember WHERE id IN ("
        "  SELECT id FROM ("
        "    SELECT id, ROW_NUMBER() OVER ("
        "      PARTITION BY user_id, channel_id"
        "      ORDER BY (role = 'admin') DESC, id"
        "    ) AS rn FROM channelmember"
        "  ) ranked WHERE rn > 1"
        ")"
    )
    db.execute_sql(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "channelmember_user_id_channel_id ON channelmember (user_id, channel_id)"
    )
    db.execute_sql(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "message_conversation_id_created_at ON message (conversation_id, created_at)"
    )


def rollback():
    """Drop both indexes (removed duplicates are not restored)."""
    db.execute_sql(
        "DROP INDEX CONCURRENTLY IF EXISTS message_conversation_id_created_at"
    )
    db.execute_sql("DROP INDEX CONCURRENTLY IF EXISTS channelmember_user_id_channel_id")
