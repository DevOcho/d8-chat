"""0012_website_lead_paging.py

Website leads from D8 page the SDRs (ONCALL_PLAN.md, "Website leads").

Schema:
    * ``escalationpolicy.alarm``: what the pager plays for the policy's
      incidents, ``siren`` (the default, every existing policy) or
      ``ka-ching``. ``IF NOT EXISTS`` makes it a no-op on a DB ``init_db.py``
      built from the current models.

Seeds (idempotent):
    * ``d8-bot`` User, inactive (cannot log in), the author of D8's page
      requests through ``POST /api/v1/internal/notify``.
    * The leads channel (``#website-leads`` unless ``WEBSITE_LEADS_CHANNEL``
      says otherwise) + its Conversation, with the bot and every workspace
      admin as members.
    * The "Website leads" policy on it, alarm ``ka-ching``: L1 group ``sdr``
      at once, L2 group ``ceo`` 15 minutes later. Left alone if the channel
      already has a policy.

Group membership comes from the Authentik ``groups`` claim at SSO login (or
is set by an admin in the user editor).
"""

# pylint: disable=C0103

import os

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    EscalationLevel,
    EscalationPolicy,
    User,
    Workspace,
    WorkspaceMember,
)
from db_bootstrap import db

D8_BOT_USERNAME = "d8-bot"
LEADS_CHANNEL_NAME = os.environ.get("WEBSITE_LEADS_CHANNEL") or "website-leads"
LEADS_LEVELS = ((1, "sdr", 0), (2, "ceo", 15))


def _default_workspace():
    workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
    if workspace is None:
        workspace = Workspace.select().order_by(Workspace.id).first()
    return workspace


def migrate():
    """Add the alarm column, then seed the bot, channel and policy."""
    db.execute_sql(
        "ALTER TABLE escalationpolicy "
        "ADD COLUMN IF NOT EXISTS alarm VARCHAR(16) NOT NULL DEFAULT 'siren'"
    )
    workspace = _default_workspace()
    if workspace is None:
        # Nothing to seed against: a fresh init_db.py run creates it all.
        return

    bot, _ = User.get_or_create(
        username=D8_BOT_USERNAME,
        defaults={
            "email": "d8-bot@d8chat.local",
            "display_name": "D8",
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=bot, workspace=workspace, defaults={"role": "member"}
    )
    channel, _ = Channel.get_or_create(
        workspace=workspace,
        name=LEADS_CHANNEL_NAME,
        defaults={
            "is_private": False,
            "topic": "Website leads from D8. A new lead pages the SDRs.",
            "description": (
                "Every contact form fill on devocho.com that D8 takes in. It "
                "pages every SDR until one acknowledges, and the CEO after 15 "
                "minutes."
            ),
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
    )
    ChannelMember.get_or_create(user=bot, channel=channel)
    admins = (
        User.select()
        .join(WorkspaceMember)
        .where(
            (WorkspaceMember.workspace == workspace) & (WorkspaceMember.role == "admin")
        )
    )
    for admin in admins:
        ChannelMember.get_or_create(user=admin, channel=channel)

    if EscalationPolicy.get_or_none(EscalationPolicy.channel == channel) is None:
        policy = EscalationPolicy.create(
            name="Website leads",
            channel=channel,
            no_oncall_start_level=None,
            alarm="ka-ching",
        )
        for level, group, delay in LEADS_LEVELS:
            EscalationLevel.create(
                policy=policy,
                level=level,
                delay_minutes=delay,
                target_type="group",
                target_group=group,
            )


def rollback():
    """Drop the policy and the alarm column. The channel, its messages and
    the bot stay: they are history."""
    channel = Channel.get_or_none(Channel.name == LEADS_CHANNEL_NAME)
    if channel is not None:
        EscalationPolicy.delete().where(EscalationPolicy.channel == channel).execute()
    db.execute_sql("ALTER TABLE escalationpolicy DROP COLUMN IF EXISTS alarm")
