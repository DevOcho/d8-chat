#!/usr/bin/env python3

from app import create_app
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    User,
    Workspace,
    WorkspaceMember,
    db,
)


def seed_data():
    """Set up minimal dev users: update admin password, create kp user."""
    workspace, _ = Workspace.get_or_create(name="DevOcho")

    # Update admin password (created by init_db.py with a random password)
    admin = User.get_or_none(User.username == "admin")
    if admin:
        admin.set_password("d8_admin")
        admin.save()
        print("Updated admin password.")
    else:
        print("WARNING: admin user not found — run `auto init` first.")

    # Get default channels
    general = Channel.get_or_none(
        Channel.name == "general", Channel.workspace == workspace
    )
    announcements = Channel.get_or_none(
        Channel.name == "announcements", Channel.workspace == workspace
    )

    # Create kp user
    kp, created = User.get_or_create(
        username="kp",
        defaults={"email": "kp@example.com", "display_name": "KP", "is_active": True},
    )
    if created:
        kp.set_password("kp555")
        kp.save()
        print("Created user 'kp'.")

        WorkspaceMember.get_or_create(
            user=kp, workspace=workspace, defaults={"role": "member"}
        )

        for channel in [general, announcements]:
            if channel:
                ChannelMember.get_or_create(user=kp, channel=channel)
                Conversation.get_or_create(
                    conversation_id_str=f"channel_{channel.id}",
                    defaults={"type": "channel"},
                )
    else:
        print("User 'kp' already exists.")

    print("Seeding complete.")


if __name__ == "__main__":
    app = create_app(start_listener=False)
    with app.app_context():
        with db.atomic():
            seed_data()
