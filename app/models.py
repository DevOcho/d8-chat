# app/models.py
# This should work for both SQLite and PostgreSQL
import datetime
import os

from peewee import (
    Model,
    Proxy,
    TextField,
    CharField,
    BooleanField,
    DateTimeField,
    ForeignKeyField,
    BigIntegerField,
    IdentityField,
    AutoField,
    SQL,
    CompositeKey,
    DeferredForeignKey,
)
from playhouse.db_url import connect
from urllib.parse import urlparse
from config import Config

from app.services import minio_service

db = Proxy()

IS_RUNNING_TESTS = "PYTEST_CURRENT_TEST" in os.environ
PrimaryKeyField = AutoField if IS_RUNNING_TESTS else IdentityField


def initialize_db(app):
    """
    Initializes the database connection using the URI from the app's config.
    This function should be called from the app factory.
    """
    db_url = app.config["DATABASE_URI"]
    # The `connect` function from playhouse correctly handles different
    # database schemes (postgres, sqlite, etc.)
    database = connect(db_url)
    db.initialize(database)


class BaseModel(Model):
    created_at = DateTimeField(default=datetime.datetime.now)
    updated_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        database = db

    def save(self, *args, **kwargs):
        self.updated_at = datetime.datetime.now()
        return super(BaseModel, self).save(*args, **kwargs)


class Workspace(BaseModel):
    id = PrimaryKeyField()
    name = CharField(unique=True)


class User(BaseModel):
    id = PrimaryKeyField()
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
    timezone = CharField(null=True, default="AST")
    presence_status = CharField(default="online")  # 'online', 'away', or 'busy'
    theme = CharField(default="system")  # 'light', 'dark', or 'system'
    wysiwyg_enabled = BooleanField(default=False, null=False)
    avatar = DeferredForeignKey("UploadedFile", backref="user_avatar", null=True)

    @property
    def avatar_url(self):
        """Returns a presigned URL for the user's avatar, or None."""
        if self.avatar:
            return minio_service.get_presigned_url(self.avatar.stored_filename)
        return None


class WorkspaceMember(BaseModel):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="workspaces")
    workspace = ForeignKeyField(Workspace, backref="members")
    role = CharField(default="member")


class Channel(BaseModel):
    id = PrimaryKeyField()
    workspace = ForeignKeyField(Workspace, backref="channels")
    name = CharField(max_length=80)
    topic = TextField(null=True)
    description = TextField(null=True)
    created_by = ForeignKeyField(User, backref="created_channels", null=True)
    is_private = BooleanField(default=False)
    posting_restricted_to_admins = BooleanField(default=False)
    invites_restricted_to_admins = BooleanField(default=False)

    class Meta:
        constraints = [SQL("UNIQUE(workspace_id, name)")]


class ChannelMember(BaseModel):
    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="channels")
    channel = ForeignKeyField(Channel, backref="members")
    role = CharField(default="member")


# This table will represent a "chat room", which can be a channel or a DM
class Conversation(BaseModel):
    id = PrimaryKeyField()
    # A conversation_id string like "channel_1" or "dm_4_5"
    conversation_id_str = CharField(unique=True)
    # The type of conversation
    type = CharField()  # 'channel' or 'dm'


class Message(BaseModel):
    id = PrimaryKeyField()
    conversation = ForeignKeyField(Conversation, backref="messages")
    user = ForeignKeyField(User, backref="messages")
    content = TextField()
    is_edited = BooleanField(default=False)
    parent_message = ForeignKeyField("self", backref="replies", null=True)

    @property
    def attachments(self):
        """Returns a query for all UploadedFile objects attached to this message."""
        return (
            UploadedFile.select()
            .join(MessageAttachment)
            .where(MessageAttachment.message == self)
        )


class Reaction(BaseModel):
    """Tracks an emoji reaction from a user on a specific message."""

    user = ForeignKeyField(User, backref="reactions")
    message = ForeignKeyField(Message, backref="reactions", on_delete="CASCADE")
    emoji = CharField()  # Stores the actual unicode emoji character

    class Meta:
        # A user can only react with the same emoji once per message
        primary_key = CompositeKey("user", "message", "emoji")


class Mention(BaseModel):
    """
    Tracks when a user is mentioned in a message.
    This allows for targeted notifications.
    """

    user = ForeignKeyField(User, backref="mentions")
    message = ForeignKeyField(Message, backref="mentions", on_delete="CASCADE")

    class Meta:
        # A user can only be mentioned once per message
        primary_key = CompositeKey("user", "message")


class UserConversationStatus(BaseModel):
    user = ForeignKeyField(User, backref="conversation_statuses")
    conversation = ForeignKeyField(Conversation, backref="user_statuses")
    last_read_timestamp = DateTimeField(default=datetime.datetime.now)
    last_notified_timestamp = DateTimeField(null=True)
    last_seen_mention_id = BigIntegerField(null=True)

    class Meta:
        # Ensures a user has only one status per conversation
        primary_key = CompositeKey("user", "conversation")


class UploadedFile(BaseModel):
    id = PrimaryKeyField()
    uploader = ForeignKeyField(User, backref="files")
    original_filename = CharField()
    stored_filename = CharField(unique=True)  # The UUID-based name
    mime_type = CharField()
    file_size_bytes = BigIntegerField()
    scan_status = CharField(default="pending")  # pending, clean, infected

    @property
    def url(self):
        """Returns a presigned URL for the file."""
        return minio_service.get_presigned_url(self.stored_filename)


class MessageAttachment(BaseModel):
    """A through model to link Messages and UploadedFiles (many-to-many)."""

    message = ForeignKeyField(Message, backref="message_links")
    attachment = ForeignKeyField(UploadedFile, backref="file_links")

    class Meta:
        primary_key = CompositeKey("message", "attachment")
