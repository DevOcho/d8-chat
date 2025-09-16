# init_db.py

import datetime
import secrets

from app import create_app
from app.models import (
    db,
    User,
    Workspace,
    WorkspaceMember,
    Channel,
    ChannelMember,
    Message,
    Conversation,
    UploadedFile,
    UserConversationStatus,
    Mention,
    MessageAttachment,
    Reaction,
    Hashtag,
    MessageHashtag,
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
]

def initialize_tables():
    """Creates all application tables if they don't already exist."""
    print("Creating tables if they don't exist...")
    with db.atomic():
        db.create_tables(ALL_MODELS, safe=True)
    print("Tables created successfully.")


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
        # Generate a secure, random password
        temp_password = secrets.token_urlsafe(16)
        admin_user.set_password(temp_password)
        admin_user.save()

        # Add admin to the workspace with the 'admin' role
        WorkspaceMember.create(user=admin_user, workspace=workspace, role="admin")

        # Add admin to the default channels
        ChannelMember.create(user=admin_user, channel=general_channel)
        ChannelMember.create(user=admin_user, channel=announcements_channel)

        print("\n" + "=" * 50)
        print("  ADMIN USER CREATED  ".center(50, "="))
        print("=" * 50)
        print(f"  Username: {admin_user.username}")
        print(f"  Password: {temp_password}")
        print("=" * 50)
        print("  Please use these credentials to log in for the first time.  \n")
    else:
        print("Admin user already exists, skipping creation.")

    print("Initial data seeded.")


if __name__ == "__main__":
    # Step 1: Create a Flask app instance to get the application context.
    # This will initialize our 'db' proxy to connect to the correct PostgreSQL DB.
    app = create_app()

    # Step 2: All database operations must now happen within the app context.
    with app.app_context():
        initialize_tables()
        seed_initial_data()

    print("\nDatabase setup complete.")
