# app/models.py
"""Database models for the application."""

# pylint: disable=too-few-public-methods

import datetime
import os

import bcrypt
from flask_login import UserMixin
from peewee import (
    SQL,
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    CompositeKey,
    DateTimeField,
    DeferredForeignKey,
    ForeignKeyField,
    IdentityField,
    Model,
    Proxy,
    TextField,
    fn,
)
from playhouse.db_url import connect

from app.services import minio_service


def utc_now() -> datetime.datetime:
    """
    Project-wide "now" helper — naive UTC.

    Every timestamp stored or compared in this app should funnel through here
    instead of calling ``datetime.datetime.now()`` (which returns the server's
    local time and silently breaks if you redeploy to a different timezone).
    Naive (no ``tzinfo``) so it round-trips cleanly through Peewee's
    ``DateTimeField`` — switching to timezone-aware datetimes later only
    requires changing this function and the field type.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


db = Proxy()

IS_RUNNING_TESTS = "PYTEST_CURRENT_TEST" in os.environ
PrimaryKeyField = AutoField if IS_RUNNING_TESTS else IdentityField


def initialize_db(app):
    """
    Initializes the database connection using the URI from the app's config.
    Uses a connection pool for PostgreSQL to prevent connection exhaustion.
    """
    db_url = app.config["DATABASE_URI"]
    if db_url.startswith(("postgresql://", "postgres://")):
        scheme = db_url.split("://")[0]
        pool_url = db_url.replace(f"{scheme}://", f"{scheme}+pool://", 1)
        database = connect(pool_url, max_connections=15, stale_timeout=300)
    else:
        database = connect(db_url)
    db.initialize(database)


class BaseModel(Model):
    """Base model providing created_at and updated_at fields."""

    created_at = DateTimeField(default=utc_now)
    updated_at = DateTimeField(default=utc_now)

    class Meta:
        """Peewee Meta class."""

        database = db

    def save(self, *args, **kwargs):
        self.updated_at = utc_now()
        return super().save(*args, **kwargs)


class Workspace(BaseModel):
    """Represents a workspace containing channels and users."""

    id = PrimaryKeyField()
    name = CharField(unique=True)


class User(BaseModel, UserMixin):
    """Represents a user of the chat application."""

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
    # Free-text label displayed on the profile page (e.g. "EST", "Europe/Paris").
    # Not wired into any datetime rendering — message timestamps are always
    # formatted in the viewer's browser timezone via JS in chat.js. Default is
    # None so new users see "Not set" rather than a misleading hardcoded TLA.
    timezone = CharField(null=True, default=None)
    presence_status = CharField(default="online")  # 'online', 'away', or 'busy'
    theme = CharField(default="system")  # 'light', 'dark', or 'system'
    wysiwyg_enabled = BooleanField(default=False, null=False)
    last_threads_view_at = DateTimeField(null=True)
    avatar = DeferredForeignKey("UploadedFile", backref="user_avatar", null=True)
    notification_sound = CharField(default="d8-notification.mp3")

    @classmethod
    def get_active_by_id(cls, user_id):
        """
        Look up a user by primary key, returning ``None`` if the row is missing
        or the account has been deactivated.

        This is the canonical hook every auth entry point should use when
        rehydrating a session: a deactivated user must not continue an
        existing session even if they still hold a valid session cookie or
        API token. Returning ``None`` instead of raising lets the caller
        fold this into existing "unauthenticated" handling.
        """
        if user_id is None:
            return None
        user = cls.get_or_none(cls.id == user_id)
        if user is None or not user.is_active:
            return None
        return user

    def set_password(self, password):
        """Hashes the password and stores it."""
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, password):
        """Checks if the provided password matches the stored hash."""
        if self.password_hash:
            return bcrypt.checkpw(
                password.encode("utf-8"), self.password_hash.encode("utf-8")
            )
        return False

    @property
    def avatar_url(self):
        """Returns a presigned URL for the user's avatar, or None."""
        if self.avatar:
            # pylint: disable=no-member
            return minio_service.get_presigned_url(self.avatar.stored_filename)
        return None


class WorkspaceMember(BaseModel):
    """Links users to workspaces with a specific role."""

    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="workspaces")
    workspace = ForeignKeyField(Workspace, backref="members")
    role = CharField(default="member")


class Channel(BaseModel):
    """Represents a channel where multiple users can chat."""

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
        """Peewee Meta class."""

        constraints = [SQL("UNIQUE(workspace_id, name)")]


class ChannelMember(BaseModel):
    """Links users to channels with a specific role."""

    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="channels")
    channel = ForeignKeyField(Channel, backref="members")
    role = CharField(default="member")


# This table will represent a "chat room", which can be a channel or a DM
class Conversation(BaseModel):
    """Represents a conversation container for messages."""

    id = PrimaryKeyField()
    # A conversation_id string like "channel_1" or "dm_4_5"
    conversation_id_str = CharField(unique=True)
    # The type of conversation
    type = CharField()  # 'channel' or 'dm'


class Message(BaseModel):
    """Represents a chat message sent in a conversation."""

    id = PrimaryKeyField()
    conversation = ForeignKeyField(Conversation, backref="messages")
    user = ForeignKeyField(User, backref="messages", null=True)
    content = TextField()
    is_edited = BooleanField(default=False)
    parent_message = ForeignKeyField("self", backref="replies", null=True)
    reply_type = CharField(null=True)  # Can be 'quote' or 'thread'
    last_reply_at = DateTimeField(null=True)
    quoted_message = DeferredForeignKey("Message", backref="quotes", null=True)

    @property
    def attachments(self):
        """Returns a query for all UploadedFile objects attached to this message."""
        return (
            UploadedFile.select()
            .join(MessageAttachment)
            .where(MessageAttachment.message == self)
        )

    @property
    def thread_participants(self):
        """
        Returns a query for the 3 most recent, unique users who replied
        to this message in a thread.
        """
        return (
            User.select()
            .join(Message, on=User.id == Message.user)
            .where((Message.parent_message == self) & (Message.reply_type == "thread"))
            .group_by(User.id)
            .order_by(fn.MAX(Message.created_at).desc())
            .limit(3)
        )


class Reaction(BaseModel):
    """Tracks an emoji reaction from a user on a specific message."""

    user = ForeignKeyField(User, backref="reactions")
    message = ForeignKeyField(Message, backref="reactions", on_delete="CASCADE")
    emoji = CharField()  # Stores the actual unicode emoji character

    class Meta:
        """Peewee Meta class."""

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
        """Peewee Meta class."""

        # A user can only be mentioned once per message
        primary_key = CompositeKey("user", "message")


class Hashtag(BaseModel):
    """Stores a unique hashtag name."""

    id = PrimaryKeyField()
    name = CharField(unique=True, index=True)  # e.g., "devocho-life"


class MessageHashtag(BaseModel):
    """A through model to link Messages and Hashtags (many-to-many)."""

    message = ForeignKeyField(Message, backref="hashtag_links", on_delete="CASCADE")
    hashtag = ForeignKeyField(Hashtag, backref="message_links", on_delete="CASCADE")

    class Meta:
        """Peewee Meta class."""

        primary_key = CompositeKey("message", "hashtag")


class UserConversationStatus(BaseModel):
    """Tracks the read/notification status of a user in a conversation."""

    user = ForeignKeyField(User, backref="conversation_statuses")
    conversation = ForeignKeyField(Conversation, backref="user_statuses")
    last_read_timestamp = DateTimeField(default=utc_now)
    last_notified_timestamp = DateTimeField(null=True)
    last_seen_mention_id = BigIntegerField(null=True)

    class Meta:
        """Peewee Meta class."""

        # Ensures a user has only one status per conversation
        primary_key = CompositeKey("user", "conversation")


class UploadedFile(BaseModel):
    """Tracks metadata for files uploaded to Minio."""

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
        return minio_service.get_presigned_url(
            self.stored_filename,
            response_headers={
                "response-content-disposition": f'attachment; filename="{self.original_filename}"'
            },
        )


class MessageAttachment(BaseModel):
    """A through model to link Messages and UploadedFiles (many-to-many)."""

    message = ForeignKeyField(Message, backref="message_links")
    attachment = ForeignKeyField(UploadedFile, backref="file_links")

    class Meta:
        """Peewee Meta class."""

        primary_key = CompositeKey("message", "attachment")


class Poll(BaseModel):
    """Represents a poll associated with a message."""

    id = PrimaryKeyField()
    # A poll is a special type of message. This links them.
    message = ForeignKeyField(Message, backref="poll", unique=True)
    question = TextField()
    # We could add things here later, like multi-vote allowance or an expiry date.


class PollOption(BaseModel):
    """Represents a selectable option within a poll."""

    id = PrimaryKeyField()
    poll = ForeignKeyField(Poll, backref="options", on_delete="CASCADE")
    text = TextField()


class Vote(BaseModel):
    """Tracks a user's vote on a specific poll option."""

    # A user can only vote once per option in a given poll.
    user = ForeignKeyField(User, backref="votes")
    option = ForeignKeyField(PollOption, backref="votes", on_delete="CASCADE")

    class Meta:
        """Peewee Meta class."""

        primary_key = CompositeKey("user", "option")


class AuditLog(BaseModel):
    """
    Append-only record of security- and compliance-relevant actions.

    The ``actor`` is whoever ``g.user`` was at the time of the request (null
    for system events). ``action`` is a short dotted identifier like
    ``user.deactivated`` or ``channel.member_role_changed``. ``target_type`` /
    ``target_id`` point at the affected row when applicable. ``details`` is a
    JSON string for anything else worth keeping (old/new values, parameters,
    etc.) — kept as TextField so it works on SQLite as well as Postgres.

    Append-only by convention, not by DB constraint. Don't expose mutation
    routes; queries should be read-only.
    """

    id = PrimaryKeyField()
    actor = ForeignKeyField(User, backref="audit_actions", null=True)
    action = CharField(max_length=80, index=True)
    target_type = CharField(null=True)
    target_id = BigIntegerField(null=True)
    details = TextField(null=True)
    ip = CharField(null=True)

    class Meta:
        """Peewee Meta class."""

        # Index on (actor, created_at) for "what did this admin do recently"
        # lookups, and on action for "who triggered X" lookups.
        indexes = ((("actor", "created_at"), False),)
