# app/models.py
import datetime
from peewee import (
    Model, PostgresqlDatabase, TextField, CharField, BooleanField,
    DateTimeField, ForeignKeyField, BigIntegerField, IdentityField, SQL,
    CompositeKey
)
from urllib.parse import urlparse
from config import Config

db = PostgresqlDatabase(None)

class BaseModel(Model):
    created_at = DateTimeField(default=datetime.datetime.now)
    updated_at = DateTimeField(default=datetime.datetime.now)
    class Meta:
        database = db
    def save(self, *args, **kwargs):
        self.updated_at = datetime.datetime.now()
        return super(BaseModel, self).save(*args, **kwargs)

class Workspace(BaseModel):
    id = IdentityField()
    name = CharField(unique=True)

class User(BaseModel):
    id = IdentityField()
    username = CharField(unique=True)
    email = CharField(unique=True)
    password_hash = CharField(null=True)
    sso_provider = CharField(null=True)
    sso_id = CharField(null=True, unique=True)
    is_active = BooleanField(default=True)
    display_name = CharField(null=True)
    profile_picture_url = CharField(null=True)
    country = CharField(null=True)
    city = CharField(null=True)
    timezone = CharField(null=True, default='AST')
    presence_status = CharField(default='online') # 'online', 'away', or 'busy'
    theme = CharField(default='system') # 'light', 'dark', or 'system'

class WorkspaceMember(BaseModel):
    id = IdentityField()
    user = ForeignKeyField(User, backref='workspaces')
    workspace = ForeignKeyField(Workspace, backref='members')
    role = CharField(default='member')

class Channel(BaseModel):
    id = IdentityField()
    workspace = ForeignKeyField(Workspace, backref='channels')
    name = CharField(max_length=80)
    topic = TextField(null=True)
    is_private = BooleanField(default=False)
    class Meta:
        constraints = [SQL('UNIQUE(workspace_id, name)')]

class ChannelMember(BaseModel):
    id = IdentityField()
    user = ForeignKeyField(User, backref='channels')
    channel = ForeignKeyField(Channel, backref='members')

# This table will represent a "chat room", which can be a channel or a DM
class Conversation(BaseModel):
    id = IdentityField()
    # A conversation_id string like "channel_1" or "dm_4_5"
    conversation_id_str = CharField(unique=True)
    # The type of conversation
    type = CharField() # 'channel' or 'dm'

class Message(BaseModel):
    id = IdentityField()
    # Every message belongs to a single Conversation
    conversation = ForeignKeyField(Conversation, backref='messages')
    user = ForeignKeyField(User, backref='messages')
    content = TextField()
    is_edited = BooleanField(default=False)
    parent_message = ForeignKeyField('self', backref='replies', null=True)

class Mention(BaseModel):
    """
    Tracks when a user is mentioned in a message.
    This allows for targeted notifications.
    """
    user = ForeignKeyField(User, backref='mentions')
    message = ForeignKeyField(Message, backref='mentions')

    class Meta:
        # A user can only be mentioned once per message
        primary_key = CompositeKey('user', 'message')

class UserConversationStatus(BaseModel):
    user = ForeignKeyField(User, backref='conversation_statuses')
    conversation = ForeignKeyField(Conversation, backref='user_statuses')
    last_read_timestamp = DateTimeField(default=datetime.datetime.now)

    class Meta:
        # Ensures a user has only one status per conversation
        primary_key = CompositeKey('user', 'conversation')

# Function to initialize the database connection
def initialize_db():
    """
    Initializes the database connection using the DATABASE_URL from config.
    """
    # Parse the DATABASE_URL to get connection details
    parsed_url = urlparse(Config.DATABASE_URL)

    # The database name is the part of the path after the leading '/'
    db_name = parsed_url.path[1:]

    # Initialize the database proxy with the parsed details
    db.init(
        database=db_name,
        user=parsed_url.username,
        password=parsed_url.password,
        host=parsed_url.hostname,
        port=parsed_url.port
    )
