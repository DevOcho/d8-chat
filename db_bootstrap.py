"""
Standalone Peewee database connection used by smalls migrations.

The Flask app's ``db`` (in ``app.models``) is a Peewee ``Proxy`` that's only
initialized once ``create_app()`` runs ``initialize_db()``. Smalls imports its
``model`` module at script-load time and starts using ``db`` immediately, so
we can't point it at the proxy directly — there's no Flask request context
or app context yet.

This module sets up a real Peewee database from the same ``DATABASE_URI`` /
``POSTGRES_*`` env vars the app uses, and *also* initializes the proxy in
``app.models`` so that migration files can ``from app.models import SomeModel``
and have the model resolve to the same connection.
"""

import os

from dotenv import load_dotenv
from playhouse.db_url import connect

load_dotenv()

DATABASE_URI = os.environ.get("DATABASE_URI")
if not DATABASE_URI:
    pg_user = os.environ.get("POSTGRES_USER")
    pg_password = os.environ.get("POSTGRES_PASSWORD")
    pg_host = os.environ.get("POSTGRES_HOST")
    pg_db = os.environ.get("POSTGRES_DB")
    if all([pg_user, pg_password, pg_host, pg_db]):
        DATABASE_URI = f"postgresql://{pg_user}:{pg_password}@{pg_host}:5432/{pg_db}"
    else:
        raise RuntimeError(
            "DATABASE_URI or all POSTGRES_* env vars must be set to run migrations."
        )

db = connect(DATABASE_URI)

# Bind the same connection to the app-level proxy so migration files can
# import model classes (``from app.models import AuditLog``) and have them
# point at this same database. Skip if something else already initialized it.
from app.models import db as _proxy_db  # noqa: E402

if _proxy_db.obj is None:
    _proxy_db.initialize(db)
