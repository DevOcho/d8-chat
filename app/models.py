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
    DateField,
    DateTimeField,
    DeferredForeignKey,
    ForeignKeyField,
    IdentityField,
    IntegerField,
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
    # When True (default) Enter sends a chat message and Shift+Enter inserts a
    # newline; when False, Ctrl/Cmd+Enter sends and plain Enter inserts a
    # newline. User-configurable in the Preferences menu.
    send_on_enter = BooleanField(default=True, null=False)
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
    # Set when a message that starts a thread is deleted: the row stays as a
    # "This message was deleted." placeholder so its replies stay reachable.
    is_deleted = BooleanField(default=False)

    @classmethod
    def in_timeline(cls):
        """Expression for messages shown in a conversation's main timeline:
        everything but thread replies, which live in the thread panel.

        Every page, backfill and unread query must filter on this. Without it a
        thread reply took a slot in the 30-message page (and was then hidden,
        so a busy thread could open a channel nearly empty), was rendered
        inline by older pages and the reconnect backfill, and lit up the
        conversation as unread with nothing new to see.
        """
        return cls.reply_type.is_null() | (cls.reply_type != "thread")

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


class DeviceToken(BaseModel):
    """A mobile device's push notification token.

    One row per (device, FCM token). Token is globally unique because a single
    device only ever holds one FCM token at a time — if the same token shows
    up under a new user (shared/reprovisioned hardware), the registration
    endpoint reassigns the existing row rather than creating a duplicate.

    ``platform`` is informational ("ios" / "android"); FCM is the transport
    for both, so we don't branch on it for delivery, but it's useful for
    metrics and would let us switch a platform to a direct APNs path later
    without losing tokens.

    ``last_used_at`` is bumped on each push attempt so we can prune tokens
    that have been dark for a long time without waiting for FCM to mark them
    UNREGISTERED.
    """

    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="device_tokens", on_delete="CASCADE")
    platform = CharField(max_length=16)
    token = TextField(unique=True)
    last_used_at = DateTimeField(null=True)

    class Meta:
        """Peewee Meta class."""

        # Lookups are almost always "all tokens for this user" when dispatching
        # a push, so index that explicitly.
        indexes = ((("user",), False),)


class UserGroup(BaseModel):
    """A named group a user belongs to, e.g. ``devops-lead`` or ``director``.

    Used by on-call escalation levels that target "whoever is in group X"
    instead of a hard-coded user. ``source`` records where the membership came
    from: ``sso`` rows mirror the IdP's ``groups`` claim and are rewritten on
    every SSO login (see ``app.groups.sync_sso_groups``); ``local`` rows are
    managed by admins in the user editor and never touched by SSO sync, so
    password-login deployments (and local dev) can use groups too.

    ``name`` is stored normalized (stripped, lowercased) so "DevOps-Lead" in
    Authentik and "devops-lead" on an escalation level match.
    """

    id = PrimaryKeyField()
    user = ForeignKeyField(User, backref="groups", on_delete="CASCADE")
    name = CharField(max_length=150, index=True)
    source = CharField(max_length=16, default="local")  # 'sso' or 'local'

    class Meta:
        """Peewee Meta class."""

        indexes = ((("user", "name"), True),)


class OnCallSchedule(BaseModel):
    """A weekly on-call rotation.

    Each shift starts at ``handoff_time`` on ``handoff_weekday`` in the
    schedule's ``timezone`` and lasts seven days. Members take turns in
    ``position`` order. ``anchor_date`` is the local date of a handoff on which
    the member at position 0 started a turn — the rotation index for any shift
    is ``weeks since anchor_date`` modulo the member count. Roster edits
    re-anchor so this week's person doesn't change mid-shift (see
    ``oncall_service.reanchor_schedule``).

    All math runs on local wall-clock time, so a DST timezone keeps the
    handoff at the same local hour year-round.
    """

    id = PrimaryKeyField()
    name = CharField(max_length=120, unique=True)
    timezone = CharField(max_length=64, default="America/Santo_Domingo")
    handoff_weekday = IntegerField(default=0)  # Monday = 0 … Sunday = 6
    handoff_time = CharField(max_length=5, default="08:00")  # "HH:MM", local
    anchor_date = DateField()


class OnCallScheduleMember(BaseModel):
    """One engineer's slot in a rotation; lower ``position`` goes first."""

    id = PrimaryKeyField()
    schedule = ForeignKeyField(OnCallSchedule, backref="members", on_delete="CASCADE")
    user = ForeignKeyField(User, backref="oncall_memberships", on_delete="CASCADE")
    position = IntegerField(default=0)

    class Meta:
        """Peewee Meta class."""

        indexes = ((("schedule", "user"), True),)


class OnCallOverride(BaseModel):
    """Replaces the rotation for a window of time (vacation, swap, cover).

    ``user`` null means "nobody is on call" for the window — the escalation
    policy then starts at its ``no_oncall_start_level``. When overrides
    overlap, the most recently created one wins. Times are naive UTC like
    every other timestamp in the app.
    """

    id = PrimaryKeyField()
    schedule = ForeignKeyField(OnCallSchedule, backref="overrides", on_delete="CASCADE")
    user = ForeignKeyField(
        User, backref="oncall_overrides", null=True, on_delete="CASCADE"
    )
    start_at = DateTimeField(index=True)
    end_at = DateTimeField(index=True)
    reason = CharField(max_length=255, null=True)
    created_by = ForeignKeyField(
        User, backref="created_oncall_overrides", null=True, on_delete="SET NULL"
    )


class EscalationPolicy(BaseModel):
    """Who gets paged, and in what order, for incidents raised in a channel.

    ``no_oncall_start_level``: when a schedule level resolves to nobody (empty
    rotation, or an override with no cover), paging jumps straight to this
    level instead of walking the chain level by level. Null disables the jump.
    """

    id = PrimaryKeyField()
    name = CharField(max_length=120)
    channel = ForeignKeyField(
        Channel, backref="escalation_policies", unique=True, on_delete="CASCADE"
    )
    no_oncall_start_level = IntegerField(null=True, default=3)
    is_active = BooleanField(default=True)


class EscalationLevel(BaseModel):
    """One step of an escalation policy.

    ``delay_minutes`` is how long after the previous level was paged this one
    fires (ignored for the first level, which fires immediately). Exactly one
    target is set, selected by ``target_type``:

    * ``schedule`` — whoever is on call in ``target_schedule`` right now
    * ``group``    — every active user in ``target_group`` (a ``UserGroup`` name)
    * ``user``     — ``target_user``
    """

    id = PrimaryKeyField()
    policy = ForeignKeyField(EscalationPolicy, backref="levels", on_delete="CASCADE")
    level = IntegerField()
    delay_minutes = IntegerField(default=10)
    target_type = CharField(max_length=16)
    target_schedule = ForeignKeyField(
        OnCallSchedule, backref="escalation_levels", null=True, on_delete="SET NULL"
    )
    target_group = CharField(max_length=150, null=True)
    target_user = ForeignKeyField(
        User, backref="escalation_levels", null=True, on_delete="SET NULL"
    )

    class Meta:
        """Peewee Meta class."""

        indexes = ((("policy", "level"), True),)


class Incident(BaseModel):
    """A paging incident, opened by a monitor DOWN event.

    Lifecycle: ``triggered`` (paging, escalating) → ``acknowledged`` (paging
    stopped, someone is on it) → ``resolved`` (monitor back UP, or resolved by
    hand). ``dedupe_key`` (e.g. ``uptime-kuma:12``) ties repeat DOWN / UP
    events to the open incident for the same monitor.

    Escalation state lives here so it survives worker restarts:
    ``current_level`` is the last level paged and ``next_escalation_at`` is
    when the next one fires (null at the last level or once acknowledged).
    Every worker's ticker races for due rows with a compare-and-set update, so
    each escalation happens exactly once (see ``oncall_service``).
    """

    id = PrimaryKeyField()
    policy = ForeignKeyField(
        EscalationPolicy, backref="incidents", null=True, on_delete="SET NULL"
    )
    channel = ForeignKeyField(Channel, backref="incidents", on_delete="CASCADE")
    source = CharField(max_length=32, default="uptime-kuma")
    dedupe_key = CharField(max_length=255, index=True)
    title = CharField(max_length=255)
    details = TextField(null=True)
    source_url = TextField(null=True)
    state = CharField(max_length=16, default="triggered", index=True)
    current_level = IntegerField(default=0)
    next_escalation_at = DateTimeField(null=True, index=True)
    last_page_sent_at = DateTimeField(null=True)
    acknowledged_by = ForeignKeyField(
        User, backref="acknowledged_incidents", null=True, on_delete="SET NULL"
    )
    acknowledged_at = DateTimeField(null=True)
    resolved_by = ForeignKeyField(
        User, backref="resolved_incidents", null=True, on_delete="SET NULL"
    )
    resolved_at = DateTimeField(null=True)
    message = ForeignKeyField(
        Message, backref="incidents", null=True, on_delete="SET NULL"
    )


# At most one open incident per monitor. Two DOWN webhooks racing on different
# workers would otherwise both open one: everyone paged twice, and the UP that
# resolves only one leaves the other escalating forever. A literal WHERE, not a
# bound parameter: SQLite rejects parameters in a partial index.
Incident.add_index(
    Incident.index(
        Incident.dedupe_key,
        unique=True,
        where=SQL("state IN ('triggered', 'acknowledged')"),
        name="incident_open_dedupe_key",
    )
)


class IncidentPage(BaseModel):
    """A user being paged for an incident. Pages are cumulative: everyone with
    a row keeps ringing until the incident leaves ``triggered``."""

    id = PrimaryKeyField()
    incident = ForeignKeyField(Incident, backref="pages", on_delete="CASCADE")
    user = ForeignKeyField(User, backref="incident_pages", on_delete="CASCADE")
    level = IntegerField()

    class Meta:
        """Peewee Meta class."""

        indexes = ((("incident", "user"), True),)


class IncidentEvent(BaseModel):
    """Timeline entry for an incident (triggered, paged, escalated, …).

    ``details`` is a JSON string, like ``AuditLog.details``.
    """

    id = PrimaryKeyField()
    incident = ForeignKeyField(Incident, backref="events", on_delete="CASCADE")
    kind = CharField(max_length=32)
    user = ForeignKeyField(
        User, backref="incident_events", null=True, on_delete="SET NULL"
    )
    details = TextField(null=True)
