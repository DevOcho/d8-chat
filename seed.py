#!/usr/bin/env python3

from app import create_app
from app.models import (
    db,
    User,
    Workspace,
    WorkspaceMember,
    Channel,
    ChannelMember,
    Conversation,
)
from peewee import IntegrityError
from faker import Faker

# Initialize Faker
fake = Faker()


def _seed_users_and_members(workspace):
    """Generates 200 fake users and adds them to the workspace and default channels."""
    print("\nSeeding 200 new users...")

    # First, get the default channels that every user should be a part of.
    general_channel = Channel.get(
        Channel.name == "general", Channel.workspace == workspace
    )
    announcements_channel = Channel.get(
        Channel.name == "announcements", Channel.workspace == workspace
    )

    users_created_count = 0
    for i in range(200):
        first_name = fake.unique.first_name()
        last_name = fake.unique.last_name()

        #
        # Create a unique username and email from the fake name.
        #
        username = f"{first_name.lower()}_{last_name.lower()}"
        email = f"{username}@example.com"
        display_name = f"{first_name} {last_name}"

        try:
            # Create the user record
            user = User.create(
                username=username,
                email=email,
                display_name=display_name,
                is_active=True,
            )

            #
            # The first user created (i=0) will be our admin.
            # Everyone else will be a regular member.
            #
            role = "member"
            WorkspaceMember.create(user=user, workspace=workspace, role=role)

            #
            # Add every new user to the two default channels.
            #
            ChannelMember.create(user=user, channel=general_channel)
            ChannelMember.create(user=user, channel=announcements_channel)

            users_created_count += 1
        except IntegrityError:
            # This might happen if Faker generates a name that results in a
            # duplicate username after a previous failed run.
            print(f"-> Skipping duplicate user: {username}")
            continue

    print(f"-> Created and onboarded {users_created_count} new users.")
    #
    # We can now safely assume the first user is our primary admin for seeding purposes.
    # Let's assign them as the creator for all our seeded channels.
    #
    admin_user = User.select().order_by(User.id).first()
    if admin_user:
        print(
            f"-> Assigning '{admin_user.username}' as creator for all public channels."
        )
        Channel.update(created_by=admin_user).where(
            (Channel.workspace == workspace) & (Channel.created_by.is_null())
        ).execute()


def _seed_channels(workspace):
    """Creates a large number of diverse channels for testing purposes."""
    print("\nSeeding additional channels...")
    channel_names = [
        # Project Channels
        "project-phoenix",
        "project-pegasus",
        "q3-marketing-campaign",
        "website-redesign-2025",
        "mobile-app-v3",
        "api-deprecation-taskforce",
        "tiger-team",
        "d8-chat-meta",
        # Department & Team Channels
        "engineering-all",
        "design-ux-ui",
        "product-management",
        "sales-team",
        "customer-support",
        "human-resources",
        "finance-dept",
        "it-support",
        # Location-based Channels
        "office-london",
        "office-san-francisco",
        "remote-first",
        # Guild & Help Channels
        "frontend-guild",
        "backend-guild",
        "testing-qa",
        "documentation",
        "help-python",
        "help-javascript",
        "help-css",
        "cloud-aws",
        # General & Feedback Channels
        "product-feedback",
        "feature-requests",
        "bug-reports",
        "competitive-intel",
        "wins-and-shoutouts",
        # Social & Fun Channels
        "random",
        "water-cooler",
        "starwars",
        "marvel-vs-dc",
        "gaming-lounge",
        "music-lovers",
        "pets-of-devocho",
        "book-club",
        "cooking-and-recipes",
        "sports",
        "memes",
        # Company & HR Channels
        "hiring-and-recruitment",
        "social-committee",
        "company-culture",
        "new-hires",
        "learning-and-development",
        "security-updates",
    ]

    channels_created_count = 0
    for name in channel_names:
        # Create the channel
        channel, created = Channel.get_or_create(
            workspace=workspace, name=name, defaults={"is_private": False}
        )
        if created:
            # Also create the corresponding conversation record for the channel
            Conversation.get_or_create(
                conversation_id_str=f"channel_{channel.id}",
                defaults={"type": "channel"},
            )
            channels_created_count += 1

    print(f"-> Created {channels_created_count} new public channels.")


def seed_data():
    """Populate the database with initial test data."""
    try:
        # 1. Create Workspace
        workspace, created = Workspace.get_or_create(name="DevOcho")
        if created:
            print(f"Workspace '{workspace.name}' created.")
        else:
            print(f"Workspace '{workspace.name}' already exists.")

        #
        # The new logic will seed channels first, then users.
        # This ensures the default channels exist before we try to add users to them.
        #
        _seed_channels(workspace)
        _seed_users_and_members(workspace)

        print("\nDatabase seeding complete!")

    except IntegrityError as e:
        print(f"An error occurred during seeding: {e}")
        print("Seeding may have already been completed.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    # Create a Flask app to get the application context.
    # This initializes the db proxy with the correct connection string.
    app = create_app()
    with app.app_context():
        # All database operations must happen within the app context.
        with db.atomic():
            seed_data()
