"""0003_helpdesk_channel.py

Seeds the ``#helpdesk`` channel and a dedicated ``helpdesk-bot`` user used
as the message author for posts coming through
``POST /api/v1/internal/notify``.

Creates (idempotent):
    * ``helpdesk-bot`` User — inactive (cannot log in) but valid as a
      Message.user FK. Added to the default workspace as a ``member``.
    * ``helpdesk`` Channel in the default workspace + its Conversation row.
    * ``ChannelMember`` rows for the bot and (if present) for the
      ``kp`` user so the team can test against a real account.

Safe to re-run. Pure data migration — no schema changes.
"""

# pylint: disable=C0103

from playhouse.migrate import PostgresqlMigrator
from playhouse.migrate import migrate as pw_migrate  # noqa: F401

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    User,
    Workspace,
    WorkspaceMember,
)
from db_bootstrap import db

migrator = PostgresqlMigrator(db)

HELPDESK_BOT_USERNAME = "helpdesk-bot"
HELPDESK_CHANNEL_NAME = "helpdesk"


def _default_workspace():
    workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
    if workspace is None:
        workspace = Workspace.select().order_by(Workspace.id).first()
    return workspace


def migrate():
    workspace = _default_workspace()
    if workspace is None:
        # Nothing to seed against — a fresh init_db.py run will pick this up.
        return

    bot_user, _ = User.get_or_create(
        username=HELPDESK_BOT_USERNAME,
        defaults={
            "email": "helpdesk-bot@d8chat.local",
            "display_name": "Helpdesk Bot",
            # Inactive: get_active_by_id() returns None, so this user can
            # never log in or hold a session. Internal endpoint bypasses
            # that path and uses the row only as a Message.user FK.
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=bot_user, workspace=workspace, defaults={"role": "member"}
    )

    helpdesk_channel, _ = Channel.get_or_create(
        workspace=workspace,
        name=HELPDESK_CHANNEL_NAME,
        defaults={
            "is_private": False,
            "topic": "Helpdesk ticket activity.",
            "description": (
                "Automated notifications from the office helpdesk. "
                "Admins manage who is in this channel."
            ),
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{helpdesk_channel.id}",
        defaults={"type": "channel"},
    )

    ChannelMember.get_or_create(user=bot_user, channel=helpdesk_channel)

    kp_user = User.get_or_none(User.username == "kp")
    if kp_user is not None:
        ChannelMember.get_or_create(user=kp_user, channel=helpdesk_channel)


def rollback():
    workspace = _default_workspace()
    if workspace is None:
        return
    helpdesk_channel = Channel.get_or_none(
        (Channel.workspace == workspace) & (Channel.name == HELPDESK_CHANNEL_NAME)
    )
    if helpdesk_channel is not None:
        ChannelMember.delete().where(
            ChannelMember.channel == helpdesk_channel
        ).execute()
        Conversation.delete().where(
            Conversation.conversation_id_str == f"channel_{helpdesk_channel.id}"
        ).execute()
        helpdesk_channel.delete_instance()
    bot_user = User.get_or_none(User.username == HELPDESK_BOT_USERNAME)
    if bot_user is not None:
        WorkspaceMember.delete().where(WorkspaceMember.user == bot_user).execute()
        bot_user.delete_instance()
