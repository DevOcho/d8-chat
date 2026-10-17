"""0006_uptime_bot.py

Seeds the Uptime Kuma channel (``#devops-uptime`` unless
``UPTIME_KUMA_CHANNEL`` says otherwise) and the ``uptime-bot`` user that
authors posts coming through ``POST /api/v1/integrations/uptime-kuma``.

Creates (idempotent):
    * ``uptime-bot`` User — inactive (cannot log in) but valid as a
      Message.user FK. Added to the default workspace as a ``member``.
    * The uptime channel in the default workspace + its Conversation row.
    * ``ChannelMember`` rows for the bot and every workspace admin, so the
      channel shows up for the people who will configure on-call.

Safe to re-run. Pure data migration — no schema changes.
"""

# pylint: disable=C0103

import os

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

UPTIME_BOT_USERNAME = "uptime-bot"
UPTIME_CHANNEL_NAME = os.environ.get("UPTIME_KUMA_CHANNEL") or "devops-uptime"


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
        username=UPTIME_BOT_USERNAME,
        defaults={
            "email": "uptime-bot@d8chat.local",
            "display_name": "Uptime Kuma",
            # Inactive: can never log in or hold a session; only a Message FK.
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=bot_user, workspace=workspace, defaults={"role": "member"}
    )

    channel, _ = Channel.get_or_create(
        workspace=workspace,
        name=UPTIME_CHANNEL_NAME,
        defaults={
            "is_private": False,
            "topic": "Uptime Kuma alerts. DOWN events page the on-call engineer.",
            "description": (
                "Monitor events from Uptime Kuma. A DOWN event pages whoever is "
                "on call; acknowledge it from the alert or the incident card."
            ),
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}",
        defaults={"type": "channel"},
    )

    ChannelMember.get_or_create(user=bot_user, channel=channel)
    admins = (
        User.select()
        .join(WorkspaceMember)
        .where(
            (WorkspaceMember.workspace == workspace) & (WorkspaceMember.role == "admin")
        )
    )
    for admin in admins:
        ChannelMember.get_or_create(user=admin, channel=channel)


def rollback():
    workspace = _default_workspace()
    if workspace is None:
        return
    channel = Channel.get_or_none(
        (Channel.workspace == workspace) & (Channel.name == UPTIME_CHANNEL_NAME)
    )
    if channel is not None:
        ChannelMember.delete().where(ChannelMember.channel == channel).execute()
        Conversation.delete().where(
            Conversation.conversation_id_str == f"channel_{channel.id}"
        ).execute()
        channel.delete_instance()
    bot_user = User.get_or_none(User.username == UPTIME_BOT_USERNAME)
    if bot_user is not None:
        WorkspaceMember.delete().where(WorkspaceMember.user == bot_user).execute()
        bot_user.delete_instance()
