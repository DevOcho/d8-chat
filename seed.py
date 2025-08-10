# seed.py

from app import create_app
from app.models import db, User, Workspace, WorkspaceMember, Channel, ChannelMember, Conversation
from peewee import IntegrityError

def seed_data():
    """Populate the database with initial test data."""
    try:
        # 1. Create Workspace
        workspace, created = Workspace.get_or_create(name='DevOcho')
        if created:
            print(f"Workspace '{workspace.name}' created.")
        else:
            print(f"Workspace '{workspace.name}' already exists.")

        # 2. Create Users
        users_data = [
            {'username': 'admin', 'email': 'admin@example.com'},
            {'username': 'user1', 'email': 'user1@example.com'},
            {'username': 'user2', 'email': 'user2@example.com'},
        ]
        users = {}
        for user_data in users_data:
            user, created = User.get_or_create(
                email=user_data['email'],
                defaults={'username': user_data['username']}
            )
            users[user_data['username']] = user
            if created:
                print(f"User '{user.username}' created.")
            else:
                print(f"User '{user.username}' already exists.")

        # 3. Make all users members of the workspace
        WorkspaceMember.get_or_create(user=users['admin'], workspace=workspace, defaults={'role': 'admin'})
        WorkspaceMember.get_or_create(user=users['user1'], workspace=workspace, defaults={'role': 'member'})
        WorkspaceMember.get_or_create(user=users['user2'], workspace=workspace, defaults={'role': 'member'})
        print("Assigned users to workspace.")

        print("\nDatabase seeding complete!")

    except IntegrityError as e:
        print(f"An error occurred during seeding: {e}")
        print("Seeding may have already been completed.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == '__main__':
    # Create a Flask app to get the application context.
    # This initializes the db proxy with the correct connection string.
    app = create_app()
    with app.app_context():
        # All database operations must happen within the app context.
        with db.atomic():
            seed_data()
