"""0007_oncall.py

Adds on-call paging: user groups, weekly rotations, overrides, escalation
policies and incidents, then seeds the default setup for the uptime channel.

Tables (``safe=True`` — a no-op on DBs built by ``init_db.py``, which has
the models in ``ALL_MODELS``):
    user_group, on_call_schedule, on_call_schedule_member, on_call_override,
    escalation_policy, escalation_level, incident, incident_page,
    incident_event

Seeds (idempotent, only if the uptime channel from 0006 exists):
    * "DevOps Primary" rotation — America/Santo_Domingo, Monday 08:00
      handoff, no members yet (add them in Admin → On-Call).
    * "DevOps Uptime" policy on the uptime channel: L1 the rotation,
      L2 group ``devops-lead``, L3 group ``director``, L4 group ``ceo``,
      10 minutes apart; nobody on call → start at L3.

Group membership comes from the Authentik ``groups`` claim at SSO login (or
is set by an admin in the user editor).
"""

# pylint: disable=C0103

import datetime
import os

from playhouse.migrate import PostgresqlMigrator
from playhouse.migrate import migrate as pw_migrate  # noqa: F401

from app.models import (
    Channel,
    EscalationLevel,
    EscalationPolicy,
    Incident,
    IncidentEvent,
    IncidentPage,
    OnCallOverride,
    OnCallSchedule,
    OnCallScheduleMember,
    UserGroup,
)
from db_bootstrap import db

migrator = PostgresqlMigrator(db)

TABLES = [
    UserGroup,
    OnCallSchedule,
    OnCallScheduleMember,
    OnCallOverride,
    EscalationPolicy,
    EscalationLevel,
    Incident,
    IncidentPage,
    IncidentEvent,
]

UPTIME_CHANNEL_NAME = os.environ.get("UPTIME_KUMA_CHANNEL") or "devops-uptime"
SCHEDULE_NAME = "DevOps Primary"


def _last_monday():
    """Local (UTC-4, no DST) date of the most recent Monday 08:00 handoff."""
    now_local = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=4
    )
    start = now_local.date() - datetime.timedelta(days=now_local.weekday())
    if now_local.weekday() == 0 and now_local.hour < 8:
        start -= datetime.timedelta(days=7)
    return start


def migrate():
    db.create_tables(TABLES, safe=True)

    channel = Channel.select().where(Channel.name == UPTIME_CHANNEL_NAME).first()
    if channel is None:
        return

    schedule = OnCallSchedule.get_or_none(OnCallSchedule.name == SCHEDULE_NAME)
    if schedule is None:
        schedule = OnCallSchedule.create(
            name=SCHEDULE_NAME,
            timezone="America/Santo_Domingo",
            handoff_weekday=0,
            handoff_time="08:00",
            anchor_date=_last_monday(),
        )

    if EscalationPolicy.get_or_none(EscalationPolicy.channel == channel) is None:
        policy = EscalationPolicy.create(
            name="DevOps Uptime", channel=channel, no_oncall_start_level=3
        )
        EscalationLevel.create(
            policy=policy,
            level=1,
            delay_minutes=0,
            target_type="schedule",
            target_schedule=schedule,
        )
        for level, group in ((2, "devops-lead"), (3, "director"), (4, "ceo")):
            EscalationLevel.create(
                policy=policy,
                level=level,
                delay_minutes=10,
                target_type="group",
                target_group=group,
            )


def rollback():
    db.drop_tables(list(reversed(TABLES)), safe=True)
