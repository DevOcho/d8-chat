"""0004_add_device_token.py

Adds the ``device_token`` table for mobile push notifications.

One row per device's FCM registration token. Used by ``push_service`` to
look up where to dispatch when a notification event fires for an offline
user. Tokens are globally unique — re-registering the same token under a
new user reassigns the existing row.

Existing prod DBs need ``./smalls.py migrate`` (or ``auto migrate
d8-chat``) to pick up the new table. Fresh DBs initialized via
``init_db.py`` already have it because ``ALL_MODELS`` was updated in
lockstep — running this migration on such a DB is a no-op thanks to the
``safe=True`` table-create.
"""

# pylint: disable=C0103

from playhouse.migrate import PostgresqlMigrator
from playhouse.migrate import migrate as pw_migrate  # noqa: F401

from app.models import DeviceToken
from db_bootstrap import db

migrator = PostgresqlMigrator(db)


def migrate():
    """Create the device_token table."""
    db.create_tables([DeviceToken], safe=True)


def rollback():
    """Drop the device_token table."""
    db.drop_tables([DeviceToken])
