#!/usr/bin/env python3

import argparse
import datetime
import os
import secrets
import stat

from app import create_app
from app.models import (
    AuditLog,
    Channel,
    ChannelMember,
    Conversation,
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
]


def initialize_tables():
    """Creates all application tables if they don't already exist."""
    print("Creating tables if they don't exist...")
    with db.atomic():
        db.create_tables(ALL_MODELS, safe=True)
    print("Tables created successfully.")


def drop_all_tables():
    """Drops all application tables from the database."""
    print("Dropping all tables...")
    with db.atomic():
        db.drop_tables(ALL_MODELS)
    print("Tables dropped successfully.")


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
        help="Drop all tables and recreate them from scratch before seeding.",
    )
    args = parser.parse_args()

    # Create a Flask app to get the application context.
    # This initializes our 'db' proxy to connect to the correct PostgreSQL DB.
    app = create_app(start_listener=False)

    with app.app_context():
        with db.atomic():
            # Are we starting over?
            if args.reset_db:
                drop_all_tables()

            # setup the tables
            initialize_tables()

            # Add critical applications settings
            seed_initial_data()

    print("\nDatabase setup complete.")
