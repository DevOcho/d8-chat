#!/usr/bin/env python3

import argparse
import datetime
import os
import secrets
import stat
from urllib.parse import urlparse

from playhouse.db_url import connect
from psycopg2 import sql

from app import create_app
from app.models import (
    AuditLog,
    Channel,
    ChannelMember,
    Conversation,
    DeviceToken,
    Hashtag,
    Mention,
    Message,
    MessageAttachment,
    MessageHashtag,
    Poll,
    PollOption,
    Reaction,
    UploadedFile,
    User,
    UserConversationStatus,
    Vote,
    Workspace,
    WorkspaceMember,
    db,
)
from config import Config

ALL_MODELS = [
    User,
    Workspace,
    WorkspaceMember,
    Channel,
    ChannelMember,
    Message,
    Conversation,
    UserConversationStatus,
    Mention,
    MessageAttachment,
    Reaction,
    UploadedFile,
    Hashtag,
    MessageHashtag,
    Poll,
    PollOption,
    Vote,
    AuditLog,
    DeviceToken,
]


def initialize_tables():
    """Creates all application tables if they don't already exist."""
    print("Creating tables if they don't exist...")
    with db.atomic():
        db.create_tables(ALL_MODELS, safe=True)
    print("Tables created successfully.")


def _maintenance_connection():
    """
    Open an autocommit connection to the ``postgres`` maintenance database and
    return ``(peewee_db, raw_connection, target_db_name)``.

    ``CREATE``/``DROP DATABASE`` can't run inside a transaction, hence
    autocommit. The target database name is taken from ``Config.DATABASE_URI``
    and may contain characters (e.g. the hyphen in ``d8-chat``) that must be
    quoted as an identifier — callers use ``psycopg2.sql.Identifier`` for that.
    """
    parsed = urlparse(Config.DATABASE_URI)
    name = parsed.path.lstrip("/")
    maintenance_url = (
        f"postgresql://{parsed.username}:{parsed.password}"
        f"@{parsed.hostname}:{parsed.port or 5432}/postgres"
    )
    maintenance = connect(maintenance_url)
    maintenance.connect()
    raw = maintenance.connection()  # underlying psycopg2 connection
    raw.autocommit = True
    return maintenance, raw, name


def ensure_database_exists():
    """
    Create the target database if it does not already exist. Non-destructive —
    this is the path used outside development so existing data is never lost.
    """
    maintenance, raw, name = _maintenance_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone():
                print(f"Database {name} already exists.")
            else:
                print(f"Creating database {name}...")
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        maintenance.close()


def recreate_database():
    """
    Drop and recreate the target database so local development always starts
    from a clean slate. Any other clients (notably the running app pod) are
    disconnected first, otherwise ``DROP DATABASE`` fails with "database is
    being accessed by other users". Gated to development by the caller — this
    must never run against a real database.
    """
    maintenance, raw, name = _maintenance_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            print(f"Dropping database {name} (if it exists)...")
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )
            print(f"Creating database {name}...")
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        maintenance.close()


def seed_initial_data():
    """Creates the default workspace and channels."""
    print("Seeding initial data...")
    workspace, _ = Workspace.get_or_create(name="DevOcho")
    general_channel, _ = Channel.get_or_create(
        workspace=workspace,
        name="general",
        defaults={
            "is_private": False,
            "topic": "General announcements and discussions.",
            "description": "This is the default channel for everyone in the workspace.",
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{general_channel.id}",
        defaults={"type": "channel"},
    )
    announcements_channel, _ = Channel.get_or_create(
        workspace=workspace,
        name="announcements",
        defaults={
            "is_private": False,
            "topic": "Company-wide announcements.",
            "description": "Important, must-read announcements will be posted here.",
            "posting_restricted_to_admins": True,
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{announcements_channel.id}",
        defaults={"type": "channel"},
    )

    # Helpdesk channel + bot user used by POST /api/v1/internal/notify.
    # Mirrored in migration 0003 for existing prod databases.
    helpdesk_bot, _ = User.get_or_create(
        username="helpdesk-bot",
        defaults={
            "email": "helpdesk-bot@d8chat.local",
            "display_name": "Helpdesk Bot",
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=helpdesk_bot, workspace=workspace, defaults={"role": "member"}
    )
    helpdesk_channel, _ = Channel.get_or_create(
        workspace=workspace,
        name="helpdesk",
        defaults={
            "is_private": False,
            "topic": "Helpdesk ticket activity.",
            "description": (
                "Automated notifications from the office helpdesk. "
                "Admins manage who is in this channel."
            ),
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{helpdesk_channel.id}",
        defaults={"type": "channel"},
    )
    ChannelMember.get_or_create(user=helpdesk_bot, channel=helpdesk_channel)

    # Create the default admin user if they don't exist
    admin_user, created = User.get_or_create(
        username="admin",
        defaults={
            "email": "admin@d8chat.com",
            "is_active": True,
            "display_name": "Admin User",
            "last_threads_view_at": datetime.datetime.now(),
        },
    )

    if created:
        # Source of the password, in priority order:
        #   1. INITIAL_ADMIN_PASSWORD env var — operator-controlled, deterministic
        #   2. Generated 16-byte URL-safe token, written to a 0600 file in
        #      ``instance/admin_credentials.txt`` so the operator has a
        #      recovery path if they miss the stdout output.
        env_password = os.environ.get("INITIAL_ADMIN_PASSWORD")
        from_env = bool(env_password)
        temp_password = env_password or secrets.token_urlsafe(16)

        admin_user.set_password(temp_password)
        admin_user.save()

        # Add admin to the workspace with the 'admin' role
        WorkspaceMember.create(user=admin_user, workspace=workspace, role="admin")

        # Add admin to the default channels
        ChannelMember.create(user=admin_user, channel=general_channel)
        ChannelMember.create(user=admin_user, channel=announcements_channel)

        creds_path = None
        if not from_env:
            from flask import current_app

            instance_dir = current_app.instance_path
            os.makedirs(instance_dir, exist_ok=True)
            creds_path = os.path.join(instance_dir, "admin_credentials.txt")
            with open(creds_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"username: {admin_user.username}\npassword: {temp_password}\n"
                )
            os.chmod(creds_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

        print("\n" + "=" * 50)
        print("  ADMIN USER CREATED  ".center(50, "="))
        print("=" * 50)
        print(f"  Username: {admin_user.username}")
        if from_env:
            print("  Password: (from $INITIAL_ADMIN_PASSWORD)")
        else:
            print(f"  Password: {temp_password}")
            print(f"  Also written to: {creds_path} (mode 0600)")
        print("=" * 50)
        print("  Use these credentials to log in for the first time. ")
        print("  Change the password from the admin UI immediately after.\n")
    else:
        print("Admin user already exists, skipping creation.")

    print("Initial data seeded.")


if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description="Initialize or reset the application database."
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help=(
            "Force a full drop-and-recreate of the database even outside "
            "development. Ignored in development, which always recreates."
        ),
    )
    args = parser.parse_args()

    # Provision the database itself (this is the front half of `auto seed`, so
    # it must handle a Postgres instance that has no `d8-chat` database yet).
    #   - development: always drop + recreate so every reset starts fresh. The
    #     dev deployment sets FLASK_ENV=development and the ephemeral init pod
    #     inherits it.
    #   - anywhere else: only create the database when it's missing, so real
    #     data is never dropped. --reset-db forces a recreate when needed.
    is_development = os.environ.get("FLASK_ENV") == "development"
    if is_development or args.reset_db:
        recreate_database()
    else:
        ensure_database_exists()

    # Create a Flask app to get the application context.
    # This initializes our 'db' proxy to connect to the correct PostgreSQL DB.
    app = create_app(start_listener=False)

    with app.app_context():
        with db.atomic():
            # setup the tables
            initialize_tables()

            # Add critical applications settings
            seed_initial_data()

    print("\nDatabase setup complete.")
