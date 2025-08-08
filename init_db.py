from app.models import db, User, Workspace, WorkspaceMember, Channel, ChannelMember, Message, Conversation, UserConversationStatus, initialize_db, Mention
from config import Config
from playhouse.db_url import connect
from urllib.parse import urlparse # Import the urlparse function

ALL_MODELS = [User, Workspace, WorkspaceMember, Channel, ChannelMember, Message, Conversation, UserConversationStatus, Mention]

def create_tables():
    """Connect to the database and create tables."""
    # Parse the main DATABASE_URL to extract its components
    parsed_url = urlparse(Config.DATABASE_URL)

    db_name = parsed_url.path[1:] # The database name (removes the leading '/')
    db_user = parsed_url.username
    db_password = parsed_url.password
    db_host = parsed_url.hostname
    db_port = parsed_url.port

    # First, connect to the default 'postgres' database to create the app's database
    # Use the parsed components to build the connection string
    maintenance_conn_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/postgres"
    conn = connect(maintenance_conn_url)
    conn.autocommit = True
    cursor = conn.cursor()

    # Check if the database already exists
    cursor.execute(f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'")
    if not cursor.fetchone():
        print(f"Creating database {db_name}...")
        cursor.execute(f'CREATE DATABASE {db_name}')
    else:
        print(f"Database {db_name} already exists.")

    cursor.close()
    conn.close()

    # Now, initialize the app's main database connection using the original DATABASE_URL
    initialize_db()

    # Drop tables in reverse order of creation to respect foreign key constraints
    print("Dropping existing tables...")
    with db:
        # We get the table names directly from the models
        table_names = [m._meta.table_name for m in reversed(ALL_MODELS)]
        for table_name in table_names:
            print(f"  - Dropping {table_name}")
            db.execute_sql(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')

    # Create the tables
    with db:
        print("Creating tables...")
        db.create_tables(ALL_MODELS)
        print("Tables created successfully.")


def init_data():
    """Create the initialization data"""

    # Create the workspace
    workspace, created = Workspace.get_or_create(name='DevOcho')
    if created:
        print(f"Workspace '{workspace.name}' created.")
    else:
        print(f"Workspace '{workspace.name}' already exists.")

    # Create the initial channels
    general_channel, created = Channel.get_or_create(
        workspace=workspace,
        name='general',
        defaults={'is_private': False, 'topic': 'General announcements and discussions.'}
    )
    if created:
        print("Channel '#general' created.")

    # Proactively create the conversation for the general channel
    Conversation.get_or_create(
        conversation_id_str=f"channel_{general_channel.id}",
        defaults={'type': 'channel'}
    )
    print("Conversation for #general created.")

    announcements_channel, created = Channel.get_or_create(
        workspace=workspace,
        name='announcements',
        defaults={'is_private': False, 'topic': 'Company-wide announcements.'}
    )
    if created:
        print("Channel '#announcements' created.")

    Conversation.get_or_create(
        conversation_id_str=f"channel_{announcements_channel.id}",
        defaults={'type': 'channel'}
    )
    print("Conversation for #announcements created.")


if __name__ == '__main__':
    create_tables()
    init_data()
