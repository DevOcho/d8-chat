# seed.py

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


def _seed_channels(workspace):
    """Creates a large number of diverse channels for testing purposes."""
    print("\nSeeding additional channels...")
    channel_names = [
        # Project Channels
        "project-phoenix", "project-pegasus", "q3-marketing-campaign",
        "website-redesign-2025", "mobile-app-v3", "api-deprecation-taskforce",
        "tiger-team", "d8-chat-meta",

        # Department & Team Channels
        "engineering-all", "design-ux-ui", "product-management", "sales-team",
        "customer-support", "human-resources", "finance-dept", "it-support",

        # Location-based Channels
        "office-london", "office-san-francisco", "remote-first",

        # Guild & Help Channels
        "frontend-guild", "backend-guild", "testing-qa", "documentation",
        "help-python", "help-javascript", "help-css", "cloud-aws",

        # General & Feedback Channels
        "product-feedback", "feature-requests", "bug-reports", "competitive-intel",
        "wins-and-shoutouts",

        # Social & Fun Channels
        "random", "water-cooler", "starwars", "marvel-vs-dc", "gaming-lounge",
        "music-lovers", "pets-of-devocho", "book-club", "cooking-and-recipes",
        "sports", "memes",

        # Company & HR Channels
        "hiring-and-recruitment", "social-committee", "company-culture",
        "new-hires", "learning-and-development", "security-updates",
    ]

    channels_created_count = 0
    for name in channel_names:
        # Create the channel
        channel, created = Channel.get_or_create(
            workspace=workspace,
            name=name,
            defaults={"is_private": False}
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

        # 2. Create Users
        users_data = [
            {"username": "admin", "email": "admin@example.com"},
            {"username": "user1", "email": "user1@example.com"},
            {"username": "user2", "email": "user2@example.com"},
        ]
        users = {}
        for user_data in users_data:
            user, created = User.get_or_create(
                email=user_data["email"], defaults={"username": user_data["username"]}
            )
            users[user_data["username"]] = user
            if created:
                print(f"User '{user.username}' created.")
            else:
                print(f"User '{user.username}' already exists.")

        # 3. Make all users members of the workspace
        WorkspaceMember.get_or_create(
            user=users["admin"], workspace=workspace, defaults={"role": "admin"}
        )
        WorkspaceMember.get_or_create(
            user=users["user1"], workspace=workspace, defaults={"role": "member"}
        )
        WorkspaceMember.get_or_create(
            user=users["user2"], workspace=workspace, defaults={"role": "member"}
        )
        print("Assigned users to workspace.")
        
        #
        # This is the new function call to seed all the extra channels.
        # It's placed here to ensure the workspace it needs already exists.
        #
        _seed_channels(workspace)


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
