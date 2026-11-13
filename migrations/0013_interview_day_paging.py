"""0013_interview_day_paging.py

The in-person interview day pages whoever has to act next, with a soft chime
(ONCALL_PLAN.md, "Interview day").

No schema change: ``escalationpolicy.alarm`` (0012) holds ``chime`` as it is,
and a page request's ``to`` names who to page without being stored apart from
the incident's pages.

Seeds (idempotent):
    * The interview day channel (``#interview-day`` unless
      ``INTERVIEW_DAY_CHANNEL`` says otherwise), **private**, since its posts
      name candidates, + its Conversation, with ``d8-bot`` and every workspace
      admin as members. Admins add HR, the graders and the engineering
      managers.
    * The "Interview day" policy on it, alarm ``chime``: L1 group ``hr`` at
      once (paged only when a request names nobody D8-Chat knows), L2 group
      ``engineering-manager`` 10 minutes later. Left alone if the channel
      already has a policy.

``d8-bot`` exists from 0012. Group membership comes from the Authentik
``groups`` claim at SSO login (or is set by an admin in the user editor).
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

D8_BOT_USERNAME = "d8-bot"
INTERVIEW_CHANNEL_NAME = os.environ.get("INTERVIEW_DAY_CHANNEL") or "interview-day"
INTERVIEW_LEVELS = ((1, "hr", 0), (2, "engineering-manager", 10))


def _default_workspace():
    workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
    if workspace is None:
        workspace = Workspace.select().order_by(Workspace.id).first()
    return workspace


def migrate():
    """Seed the channel and its policy."""
    workspace = _default_workspace()
    if workspace is None:
        # Nothing to seed against: a fresh init_db.py run creates it all.
        return

    channel, _ = Channel.get_or_create(
        workspace=workspace,
        name=INTERVIEW_CHANNEL_NAME,
        defaults={
            "is_private": True,
            "topic": "The in-person interview day. Each step pages who has to act.",
            "description": (
                "The interview platform's events on the day, relayed by the "
                "website: a candidate finishing the exercise pages their two "
                "graders, both passing pages them to start the interview, and a "
                "result pages the HR person who booked the visit. Nobody "
                "acknowledges within 10 minutes and the engineering managers "
                "are paged too."
            ),
        },
    )
    Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
    )
    bot = User.get_or_none(User.username == D8_BOT_USERNAME)
    if bot is not None:
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
            name="Interview day",
            channel=channel,
            no_oncall_start_level=None,
            alarm="chime",
        )
        for level, group, delay in INTERVIEW_LEVELS:
            EscalationLevel.create(
                policy=policy,
                level=level,
                delay_minutes=delay,
                target_type="group",
                target_group=group,
            )


def rollback():
    """Drop the policy. The channel and its messages stay: they are history."""
    channel = Channel.get_or_none(Channel.name == INTERVIEW_CHANNEL_NAME)
    if channel is not None:
        EscalationPolicy.delete().where(EscalationPolicy.channel == channel).execute()
