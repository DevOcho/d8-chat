"""A throwaway SQLite database for smalls.py in tests.

smalls.py imports its SMALLS_MODEL module at import time and uses its ``db``
at once (creating MigrationHistory). Pointing it here keeps the tests off
db_bootstrap, which would also bind the app's database proxy.
"""

from peewee import SqliteDatabase

db = SqliteDatabase(":memory:")
