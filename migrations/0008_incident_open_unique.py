"""0008_incident_open_unique.py

Allows at most one open (triggered/acknowledged) incident per ``dedupe_key``.

Two Uptime Kuma DOWN webhooks for the same monitor, handled by different
workers at the same moment, could each see no open incident and open one:
everyone was paged twice, and the UP event resolved only the newest, so the
older one kept re-paging and escalating after the monitor recovered.

Any open duplicates already present are resolved first (all but the newest per
key), or the unique index could not be built. Fresh DBs built by
``init_db.py`` / 0007 already have the index (it's on the ``Incident`` model);
``IF NOT EXISTS`` makes this a no-op there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Resolve stray open duplicates, then add the partial unique index."""
    db.execute_sql(
        "UPDATE incident SET state = 'resolved', resolved_at = NOW(), "
        "next_escalation_at = NULL "
        "WHERE state IN ('triggered', 'acknowledged') AND id NOT IN ("
        "  SELECT MAX(id) FROM incident "
        "  WHERE state IN ('triggered', 'acknowledged') GROUP BY dedupe_key"
        ")"
    )
    db.execute_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS incident_open_dedupe_key "
        "ON incident (dedupe_key) WHERE state IN ('triggered', 'acknowledged')"
    )


def rollback():
    """Drop the partial unique index."""
    db.execute_sql("DROP INDEX IF EXISTS incident_open_dedupe_key")
