from app.models import db, User, Workspace, WorkspaceMember, Channel, ChannelMember, initialize_db
from peewee import IntegrityError

def seed_data():
    """Populate the database with initial test data."""
    initialize_db()

    with db.atomic(): # Use a transaction
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
            admin_role = 'admin'
            member_role = 'member'

            WorkspaceMember.get_or_create(user=users['admin'], workspace=workspace, defaults={'role': admin_role})
            WorkspaceMember.get_or_create(user=users['user1'], workspace=workspace, defaults={'role': member_role})
            WorkspaceMember.get_or_create(user=users['user2'], workspace=workspace, defaults={'role': member_role})
            print("Assigned users to workspace.")

            # 4. Create Channels
            general_channel, created = Channel.get_or_create(
                workspace=workspace,
                name='general',
                defaults={'is_private': False, 'topic': 'General announcements and discussions.'}
            )
            if created:
                print("Channel '#general' created.")


            announcements_channel, created = Channel.get_or_create(
                workspace=workspace,
                name='announcements',
                defaults={'is_private': False, 'topic': 'Company-wide announcements.'}
            )
            if created:
                print("Channel '#announcements' created.")

            # 5. Add members to channels
            # Add all users to #general
            ChannelMember.get_or_create(user=users['admin'], channel=general_channel)
            ChannelMember.get_or_create(user=users['user1'], channel=general_channel)
            ChannelMember.get_or_create(user=users['user2'], channel=general_channel)
            print("Added all users to #general.")

            # Add all users to #announcements
            ChannelMember.get_or_create(user=users['admin'], channel=announcements_channel)
            ChannelMember.get_or_create(user=users['user1'], channel=announcements_channel)
            ChannelMember.get_or_create(user=users['user2'], channel=announcements_channel)
            print("Added all users to #announcements.")

            print("\nDatabase seeding complete!")

        except IntegrityError as e:
            print(f"An error occurred during seeding: {e}")
            print("Seeding may have already been completed.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


if __name__ == '__main__':
    seed_data()
