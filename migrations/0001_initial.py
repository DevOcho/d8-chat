"""0001_initial.py

Baseline marker. Existing prod databases were created by ``init_db.py``
(``db.create_tables(ALL_MODELS, safe=True)``) and already have all the
tables that existed when smalls was added. This migration is intentionally
a no-op so smalls can record ``0001`` in its ``MigrationHistory`` table
without trying to recreate schema that's already there.

For a *fresh* database the workflow is:
    1. ``./init_db.py``                 # creates tables, seeds default data
    2. ``./smalls.py migrate``          # records 0001 + applies later ones

Future schema changes go in numbered files after this one.
"""

# pylint: disable=C0103

from playhouse.migrate import PostgresqlMigrator
from playhouse.migrate import migrate as pw_migrate  # noqa: F401

from db_bootstrap import db

migrator = PostgresqlMigrator(db)


def migrate():
    """No-op baseline."""


def rollback():
    """No-op baseline."""
