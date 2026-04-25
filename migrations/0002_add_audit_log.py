"""0002_add_audit_log.py

Adds the ``audit_log`` table introduced during the security audit pass for
recording admin actions (user create/edit, role changes, channel
mutations, etc.). See ``app/audit.py`` for the helper that writes rows.

Existing prod DBs that were initialized before this migration need to run
``./smalls.py migrate`` (or ``auto migrate d8-chat``) to pick up the new
table. Fresh DBs initialized via ``init_db.py`` already have it because
``ALL_MODELS`` was updated in lockstep — running this migration on such a
DB is a no-op thanks to the ``safe=True`` table-create.
"""

# pylint: disable=C0103

from playhouse.migrate import PostgresqlMigrator
from playhouse.migrate import migrate as pw_migrate  # noqa: F401

from app.models import AuditLog
from db_bootstrap import db

migrator = PostgresqlMigrator(db)


def migrate():
    """Create the audit_log table."""
    db.create_tables([AuditLog], safe=True)


def rollback():
    """Drop the audit_log table."""
    db.drop_tables([AuditLog])
