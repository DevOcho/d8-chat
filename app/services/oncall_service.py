# app/services/oncall_service.py
"""On-call paging: weekly rotations, escalation policies and incidents.

Flow: Uptime Kuma POSTs a monitor event to the webhook
(``api_v1.uptime_kuma_webhook``) → ``handle_uptime_kuma_event`` posts it into
the configured channel as ``uptime-bot``. A DOWN event on a channel with an
``EscalationPolicy`` opens an ``Incident`` and pages level 1. Each gunicorn
worker runs ``run_ticker``, which escalates incidents nobody acknowledged and
re-sends page frames to everyone still being paged. An UP event (or a manual
resolve) closes the incident.

Paging rules (product decisions, see ONCALL_PLAN.md):

* Only DOWN events page. Human messages, @channel and @here never do.
* Pages are cumulative — every level paged so far keeps ringing until someone
  acknowledges, including after the last level.
* A schedule level with nobody on call jumps to the policy's
  ``no_oncall_start_level`` (the director); a level that adds nobody new (e.g.
  the team lead is on call this week) is skipped immediately.
* Anyone in the escalation chain can acknowledge or resolve.

Multi-worker safety: incident state lives in the DB and every escalation /
re-send is claimed with a compare-and-set ``UPDATE … WHERE`` so exactly one
worker wins each step, and a restart loses nothing.
"""

import datetime
import json
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, render_template
from peewee import IntegrityError

from app.background import spawn_background
from app.chat_manager import chat_manager
from app.groups import normalize_group_name, users_in_group
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    EscalationLevel,
    EscalationPolicy,
    Incident,
    IncidentEvent,
    IncidentPage,
    OnCallOverride,
    OnCallSchedule,
    OnCallScheduleMember,
    User,
    UserGroup,
    Workspace,
    WorkspaceMember,
    db,
    utc_now,
)
from app.services import push_service

UPTIME_BOT_USERNAME = "uptime-bot"
DEFAULT_TIMEZONE = "America/Santo_Domingo"
DEFAULT_SCHEDULE_NAME = "DevOps Primary"
DEFAULT_POLICY_NAME = "DevOps Uptime"
# (level, group) for the default policy; level 1 is the rotation.
DEFAULT_GROUP_LEVELS = ((2, "devops-lead"), (3, "director"), (4, "ceo"))
DEFAULT_ESCALATION_MINUTES = 10
DEFAULT_NO_ONCALL_START_LEVEL = 3

TARGET_TYPES = ("schedule", "group", "user")
OPEN_STATES = ("triggered", "acknowledged")
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
HANDOFF_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# How often each worker's ticker wakes, and how often a still-triggered
# incident's page is re-sent. Re-sending makes delivery self-healing: pub/sub
# is at-most-once, so a frame lost to a reconnect is replaced within a minute.
TICK_SECONDS = 15
PAGE_REPEAT_SECONDS = 60

# Uptime Kuma heartbeat status codes.
KUMA_DOWN, KUMA_UP, KUMA_PENDING, KUMA_MAINTENANCE = 0, 1, 2, 3


# --- Time and rotation math ---


def schedule_zone(schedule):
    """The schedule's ``ZoneInfo``; falls back to UTC on a bad name."""
    try:
        return ZoneInfo(schedule.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_local(schedule, at_utc):
    """Naive UTC → naive wall-clock time in the schedule's timezone."""
    return (
        at_utc.replace(tzinfo=datetime.timezone.utc)
        .astimezone(schedule_zone(schedule))
        .replace(tzinfo=None)
    )


def to_utc(schedule, local_naive):
    """Naive wall-clock time in the schedule's timezone → naive UTC."""
    return (
        local_naive.replace(tzinfo=schedule_zone(schedule))
        .astimezone(datetime.timezone.utc)
        .replace(tzinfo=None)
    )


def _handoff_hm(schedule):
    match = HANDOFF_TIME_RE.match(schedule.handoff_time or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def shift_start_local(schedule, at_utc):
    """Local datetime of the handoff that began the shift containing ``at_utc``."""
    local = to_local(schedule, at_utc)
    hour, minute = _handoff_hm(schedule)
    days_back = (local.weekday() - schedule.handoff_weekday) % 7
    start = datetime.datetime.combine(
        local.date() - datetime.timedelta(days=days_back), datetime.time(hour, minute)
    )
    if start > local:
        start -= datetime.timedelta(days=7)
    return start


def shift_bounds(schedule, at_utc):
    """``(start_utc, end_utc)`` of the shift containing ``at_utc``."""
    start = shift_start_local(schedule, at_utc)
    return (
        to_utc(schedule, start),
        to_utc(schedule, start + datetime.timedelta(days=7)),
    )


def rotation_members(schedule):
    """Users in rotation order (including inactive ones, so indexes stay put)."""
    return [
        m.user
        for m in OnCallScheduleMember.select(OnCallScheduleMember, User)
        .join(User)
        .where(OnCallScheduleMember.schedule == schedule)
        .order_by(OnCallScheduleMember.position, OnCallScheduleMember.id)
    ]


def rotation_user(schedule, at_utc, members=None):
    """Whose turn the rotation says it is at ``at_utc``, ignoring overrides."""
    if members is None:
        members = rotation_members(schedule)
    if not members:
        return None
    start_date = shift_start_local(schedule, at_utc).date()
    weeks = (start_date - schedule.anchor_date).days // 7
    return members[weeks % len(members)]


def active_override(schedule, at_utc):
    """The override covering ``at_utc`` (newest wins), or ``None``."""
    return (
        OnCallOverride.select()
        .where(
            (OnCallOverride.schedule == schedule)
            & (OnCallOverride.start_at <= at_utc)
            & (OnCallOverride.end_at > at_utc)
        )
        .order_by(OnCallOverride.id.desc())
        .first()
    )


def oncall_user(schedule, at_utc):
    """Who is actually on call at ``at_utc`` (overrides applied), or ``None``.

    An inactive user counts as nobody, so a departed engineer still in the
    rotation falls through to the policy's no-on-call level instead of
    paging a dead account.
    """
    override = active_override(schedule, at_utc)
    user = (
        (override.user if override.user_id else None)
        if override is not None
        else rotation_user(schedule, at_utc)
    )
    if user is None or not user.is_active:
        return None
    return user


def reanchor_schedule(schedule, keep_user_id, now=None):
    """Re-anchor after a roster or handoff change.

    Picks ``anchor_date`` so that ``keep_user_id`` (whoever the rotation had
    on call before the change) stays on call for the current shift. If they
    were removed, the member now at position 0 takes the current shift.
    """
    now = now or utc_now()
    member_ids = [u.id for u in rotation_members(schedule)]
    start_date = shift_start_local(schedule, now).date()
    index = member_ids.index(keep_user_id) if keep_user_id in member_ids else 0
    schedule.anchor_date = start_date - datetime.timedelta(days=7 * index)
    schedule.save()


def renumber_members(schedule):
    """Close gaps in ``position`` after a removal or move (0, 1, 2, …)."""
    members = (
        OnCallScheduleMember.select()
        .where(OnCallScheduleMember.schedule == schedule)
        .order_by(OnCallScheduleMember.position, OnCallScheduleMember.id)
    )
    for index, member in enumerate(members):
        if member.position != index:
            member.position = index
            member.save()


def upcoming_shifts(schedule, now=None, weeks=8):
    """The current and next ``weeks - 1`` shifts, with overrides that touch them."""
    now = now or utc_now()
    members = rotation_members(schedule)
    first_start = shift_start_local(schedule, now)
    shifts = []
    for i in range(weeks):
        start_local = first_start + datetime.timedelta(days=7 * i)
        end_local = start_local + datetime.timedelta(days=7)
        start_utc = to_utc(schedule, start_local)
        end_utc = to_utc(schedule, end_local)
        overrides = list(
            OnCallOverride.select()
            .where(
                (OnCallOverride.schedule == schedule)
                & (OnCallOverride.start_at < end_utc)
                & (OnCallOverride.end_at > start_utc)
            )
            .order_by(OnCallOverride.start_at, OnCallOverride.id)
        )
        shifts.append(
            {
                "start_local": start_local,
                "end_local": end_local,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "rotation_user": rotation_user(schedule, start_utc, members),
                "overrides": overrides,
                "is_current": i == 0,
            }
        )
    return shifts


def swap_shifts(schedule, start_a_utc, start_b_utc, created_by=None, now=None):
    """Swap two upcoming shifts by creating a full-shift override on each.

    Returns the two overrides, or ``None`` if either start isn't a shift
    boundary in the upcoming window or the shifts have the same person.
    """
    shifts = {s["start_utc"]: s for s in upcoming_shifts(schedule, now, weeks=12)}
    shift_a, shift_b = shifts.get(start_a_utc), shifts.get(start_b_utc)
    if shift_a is None or shift_b is None or start_a_utc == start_b_utc:
        return None
    user_a, user_b = shift_a["rotation_user"], shift_b["rotation_user"]
    if user_a is None or user_b is None or user_a.id == user_b.id:
        return None
    reason = f"Swap: {display_name(user_a)} ↔ {display_name(user_b)}"
    return (
        OnCallOverride.create(
            schedule=schedule,
            user=user_b,
            start_at=shift_a["start_utc"],
            end_at=shift_a["end_utc"],
            reason=reason,
            created_by=created_by,
        ),
        OnCallOverride.create(
            schedule=schedule,
            user=user_a,
            start_at=shift_b["start_utc"],
            end_at=shift_b["end_utc"],
            reason=reason,
            created_by=created_by,
        ),
    )


def oncall_status_for_user(user, now=None):
    """``{"schedule", "until_utc"}`` if ``user`` is on call right now, else ``None``."""
    if user is None:
        return None
    now = now or utc_now()
    for schedule in OnCallSchedule.select().order_by(OnCallSchedule.id):
        if (getattr(oncall_user(schedule, now), "id", None)) != user.id:
            continue
        override = active_override(schedule, now)
        until = override.end_at if override else shift_bounds(schedule, now)[1]
        return {"schedule": schedule, "until_utc": until}
    return None


# --- Escalation levels and the chain ---


def display_name(user):
    """How a user is named in bot messages and the pager."""
    if user is None:
        return "nobody"
    return user.display_name or user.username


def level_targets(level, at_utc):
    """Users a level would page at ``at_utc``."""
    if level.target_type == "schedule":
        if level.target_schedule_id is None:
            return []
        user = oncall_user(level.target_schedule, at_utc)
        return [user] if user else []
    if level.target_type == "group":
        return users_in_group(level.target_group)
    if level.target_type == "user":
        user = level.target_user if level.target_user_id else None
        return [user] if user is not None and user.is_active else []
    return []


def describe_level(level):
    """Human label for a level's target, e.g. "On call: DevOps Primary"."""
    if level.target_type == "schedule":
        name = level.target_schedule.name if level.target_schedule_id else "(missing)"
        return f"On call: {name}"
    if level.target_type == "group":
        return f"Group: {level.target_group}"
    if level.target_type == "user":
        return f"User: {display_name(level.target_user) if level.target_user_id else '(missing)'}"
    return level.target_type


def sorted_levels(policy):
    """A policy's levels, lowest first."""
    if policy is None:
        return []
    return list(
        EscalationLevel.select()
        .where(EscalationLevel.policy == policy)
        .order_by(EscalationLevel.level)
    )


def user_in_chain(incident, user):
    """True if ``user`` may acknowledge/resolve ``incident``.

    The chain is everyone the policy could page: rotation members and
    override users of any targeted schedule, members of any targeted group,
    any targeted user — plus anyone already paged for this incident (covers
    the admin fallback). Workspace admins are always allowed so an incident
    can never get stuck open.
    """
    if user is None:
        return False
    if (
        IncidentPage.select()
        .where((IncidentPage.incident == incident) & (IncidentPage.user == user))
        .exists()
    ):
        return True
    if _is_workspace_admin(user):
        return True
    for level in sorted_levels(incident.policy):
        if level.target_type == "user" and level.target_user_id == user.id:
            return True
        if level.target_type == "group" and (
            UserGroup.select()
            .where(
                (UserGroup.user == user)
                & (UserGroup.name == normalize_group_name(level.target_group))
            )
            .exists()
        ):
            return True
        if level.target_type == "schedule" and level.target_schedule_id:
            schedule = level.target_schedule_id
            if (
                OnCallScheduleMember.select()
                .where(
                    (OnCallScheduleMember.schedule == schedule)
                    & (OnCallScheduleMember.user == user)
                )
                .exists()
                or OnCallOverride.select()
                .where(
                    (OnCallOverride.schedule == schedule)
                    & (OnCallOverride.user == user)
                )
                .exists()
            ):
                return True
    return False


def _is_workspace_admin(user):
    return (
        WorkspaceMember.select()
        .where((WorkspaceMember.user == user) & (WorkspaceMember.role == "admin"))
        .exists()
    )


def _workspace_admins():
    return list(
        User.select()
        .join(WorkspaceMember)
        .where((WorkspaceMember.role == "admin") & (User.is_active))
        .distinct()
    )


# --- Serialization ---


def _user_brief(user):
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }


def _iso(value):
    return value.isoformat() if value else None


def paged_users(incident):
    """``[(user, level)]`` for everyone paged on ``incident``, in page order."""
    return [
        (page.user, page.level)
        for page in IncidentPage.select(IncidentPage, User)
        .join(User)
        .where(IncidentPage.incident == incident)
        .order_by(IncidentPage.id)
    ]


def serialize_incident(incident):
    """JSON shape shared by the web pager, the WS ``page`` frame and the API.

    Timestamps are naive-UTC ISO strings, like every other API timestamp.
    """
    channel = incident.channel
    return {
        "id": incident.id,
        "title": incident.title,
        "details": incident.details,
        "source": incident.source,
        "source_url": incident.source_url,
        "state": incident.state,
        "current_level": incident.current_level,
        "triggered_at": _iso(incident.created_at),
        "next_escalation_at": _iso(incident.next_escalation_at),
        "acknowledged_at": _iso(incident.acknowledged_at),
        "acknowledged_by": _user_brief(
            incident.acknowledged_by if incident.acknowledged_by_id else None
        ),
        "resolved_at": _iso(incident.resolved_at),
        "resolved_by": _user_brief(
            incident.resolved_by if incident.resolved_by_id else None
        ),
        "channel_id": channel.id,
        "channel_name": channel.name,
        "conversation_id_str": f"channel_{channel.id}",
        "message_id": incident.message_id,
        "paged_users": [
            dict(_user_brief(user), level=level)
            for user, level in paged_users(incident)
        ],
    }


def incident_events(incident):
    """Timeline rows with ``details`` decoded."""
    rows = []
    for event in (
        IncidentEvent.select()
        .where(IncidentEvent.incident == incident)
        .order_by(IncidentEvent.id)
    ):
        try:
            details = json.loads(event.details) if event.details else {}
        except ValueError:
            details = {}
        rows.append({"event": event, "details": details})
    return rows


def _record(incident, kind, user=None, **details):
    IncidentEvent.create(
        incident=incident,
        kind=kind,
        user=user,
        details=json.dumps(details) if details else None,
    )


# --- Bot messages ---


def uptime_bot():
    """The ``uptime-bot`` user that authors monitor and incident messages."""
    return User.get_or_none(User.username == UPTIME_BOT_USERNAME)


def post_bot_message(channel, text, on_created=None):
    """Post ``text`` into ``channel`` as ``uptime-bot`` and broadcast it.

    Mirrors the user-facing send path: persist (mention/hashtag extraction),
    broadcast over pub/sub, then badge/notification fan-out off the hot path.
    ``on_created(message)`` runs after the row exists but before the
    broadcast, so an incident can be linked to the message and render in its
    card on first paint. Returns the message, or ``None`` if the bot or the
    channel's conversation is missing.
    """
    # pylint: disable=import-outside-toplevel,cyclic-import
    from app.routes import _broadcast_regular_message
    from app.services import chat_service

    bot = uptime_bot()
    conv_id_str = f"channel_{channel.id}"
    conversation = Conversation.get_or_none(
        Conversation.conversation_id_str == conv_id_str
    )
    if bot is None or conversation is None:
        current_app.logger.error(
            "Cannot post to #%s: uptime-bot user or channel conversation missing.",
            channel.name,
        )
        return None

    message = chat_service.handle_new_message(
        sender=bot, conversation=conversation, chat_text=text
    )
    if on_created is not None:
        on_created(message)
    _broadcast_regular_message(bot, message, conv_id_str)
    spawn_background(chat_service.send_notifications_for_new_message, message, bot)
    return message


def broadcast_incident_update(incident):
    """Live-update the incident card in the channel (web) + ``incident_update`` (API)."""
    if not incident.message_id:
        return
    html = render_template("partials/incident_card.html", incident=incident, oob=True)
    try:
        chat_manager.broadcast(
            f"channel_{incident.channel_id}",
            {
                "_raw_html": html,
                "api_data": {
                    "type": "incident_update",
                    "data": serialize_incident(incident),
                },
            },
        )
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception("incident card broadcast failed")


# --- Page delivery ---


def page_frame(incident, repeat=False):
    """The ``page`` WebSocket frame for ``incident``."""
    return {
        "type": "page",
        "repeat": repeat,
        "incident": serialize_incident(incident),
    }


def _deliver_page(incident, user_ids, repeat=False):
    """Ring ``user_ids``: WS ``page`` frame on every socket + a mobile push."""
    if not user_ids:
        return
    frame = page_frame(incident, repeat=repeat)
    for user_id in user_ids:
        try:
            chat_manager.send_to_user(user_id, frame)
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception("page frame to user %s failed", user_id)
    spawn_background(_push_pages, incident.id, list(user_ids))


def _push_pages(incident_id, user_ids):
    incident = Incident.get_or_none(Incident.id == incident_id)
    if incident is None or incident.state != "triggered":
        return
    for user_id in user_ids:
        push_service.send_page(
            user_id,
            title=f"🚨 DOWN: {incident.title}",
            body=incident.details or "Production alert. Open D8-Chat to acknowledge.",
            data={
                "type": "page",
                "incident_id": incident.id,
                "conversation_id_str": f"channel_{incident.channel_id}",
                "message_id": incident.message_id or "",
            },
        )


def _deliver_page_cleared(incident, user_ids, state=None):
    """Silence ``user_ids``' pagers for ``incident`` (web + mobile).

    ``state`` overrides the incident's own state in the frame and push, for
    ``"paused"`` (paging switched off while the incident stays triggered).
    """
    state = state or incident.state
    frame = {
        "type": "page_cleared",
        "incident_id": incident.id,
        "state": state,
        "incident": serialize_incident(incident),
    }
    for user_id in user_ids:
        try:
            chat_manager.send_to_user(user_id, frame)
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception("page_cleared to user %s failed", user_id)
    spawn_background(
        _push_page_cleared,
        incident.id,
        state,
        list(user_ids),
        title=incident.title,
        acknowledged_by=(
            display_name(incident.acknowledged_by)
            if incident.acknowledged_by_id
            else None
        ),
    )


def _push_page_cleared(incident_id, state, user_ids, *, title, acknowledged_by=None):
    if state == "acknowledged" and acknowledged_by:
        alert = f"✅ {title} acknowledged by {acknowledged_by}"
    elif state == "paused":
        alert = f"🔕 {title}: paging switched off"
    else:
        alert = f"✅ {title} {state}"
    for user_id in user_ids:
        push_service.send_page_cleared(
            user_id,
            alert=alert,
            data={"type": "page_cleared", "incident_id": incident_id, "state": state},
        )


def silence_policy(policy):
    """Stop the pagers ringing for ``policy``'s triggered incidents.

    Called when an admin switches paging off. The incidents stay triggered
    (the outage is still real), but nobody is paged until it's switched back
    on, when the next tick re-pages and escalation resumes.
    """
    for incident in Incident.select().where(
        (Incident.policy == policy) & (Incident.state == "triggered")
    ):
        _deliver_page_cleared(
            incident, [u.id for u, _ in paged_users(incident)], state="paused"
        )


def send_test_page(user):
    """Ring ``user``'s pager with a fake incident (nothing is stored)."""
    now = utc_now()
    frame = {
        "type": "page",
        "test": True,
        "repeat": False,
        "incident": {
            "id": f"test-{int(time.time())}",
            "title": "Test page",
            "details": "This is a test of your D8-Chat pager. Nothing is down.",
            "source": "test",
            "source_url": None,
            "state": "triggered",
            "current_level": 1,
            "triggered_at": _iso(now),
            "channel_name": None,
            "conversation_id_str": None,
            "message_id": None,
            "paged_users": [dict(_user_brief(user), level=1)],
        },
    }
    chat_manager.send_to_user(user.id, frame)
    spawn_background(
        push_service.send_page,
        user.id,
        title="🚨 Test page",
        body="This is a test of your D8-Chat pager.",
        data={"type": "page", "test": "1", "incident_id": frame["incident"]["id"]},
    )


def _paging_enabled():
    """WHERE clause: the incident's policy hasn't had paging switched off.

    "Paging enabled = off" means nobody is paged, so it has to stop incidents
    that are already ringing too, not only new DOWN events. An incident whose
    policy was deleted keeps ringing until someone acknowledges it.
    """
    paused = EscalationPolicy.select(EscalationPolicy.id).where(
        ~EscalationPolicy.is_active
    )
    return Incident.policy.is_null() | Incident.policy.not_in(paused)


def active_pages_for(user):
    """Incidents currently paging ``user`` (state ``triggered``)."""
    return list(
        Incident.select()
        .join(IncidentPage)
        .where(
            (IncidentPage.user == user)
            & (Incident.state == "triggered")
            & _paging_enabled()
        )
        .order_by(Incident.id)
    )


# --- Incident lifecycle ---


def open_incident_for(dedupe_key):
    """The open (triggered/acknowledged) incident for ``dedupe_key``, if any."""
    return (
        Incident.select()
        .where((Incident.dedupe_key == dedupe_key) & (Incident.state.in_(OPEN_STATES)))
        .order_by(Incident.id.desc())
        .first()
    )


def open_incidents_for(dedupe_key):
    """Every open incident for ``dedupe_key``, oldest first.

    The unique index allows only one, but an UP resolves them all anyway so a
    stray duplicate can never keep paging after the monitor recovers.
    """
    return list(
        Incident.select()
        .where((Incident.dedupe_key == dedupe_key) & (Incident.state.in_(OPEN_STATES)))
        .order_by(Incident.id)
    )


def fire_level(incident, level_number, now):
    """Page level ``level_number`` of the incident's policy.

    Applies the paging rules: a schedule level with nobody on call jumps to
    ``no_oncall_start_level``; a level that adds nobody new is skipped
    immediately; if the chain runs out without anyone ever being paged,
    workspace admins are paged as a last resort. Sets ``current_level`` and
    ``next_escalation_at`` and saves. Returns the users newly paged (the
    caller delivers the page after its transaction commits).

    Recursion only ever moves to a strictly higher level, so it terminates.
    """
    levels = sorted_levels(incident.policy)
    pending = [lvl for lvl in levels if lvl.level >= level_number]
    if not pending:
        incident.next_escalation_at = None
        incident.save()
        return _page_admins_if_nobody_paged(incident)

    level = pending[0]
    later = [lvl for lvl in levels if lvl.level > level.level]
    targets = level_targets(level, now)
    incident.current_level = level.level

    jump_to = incident.policy.no_oncall_start_level
    if (
        level.target_type == "schedule"
        and not targets
        and jump_to is not None
        and jump_to > level.level
        and any(lvl.level >= jump_to for lvl in levels)
    ):
        _record(incident, "no_oncall", level=level.level, jump_to=jump_to)
        return fire_level(incident, jump_to, now)

    already = {page.user_id for page in incident.pages}
    new_users = [user for user in targets if user.id not in already]

    if not new_users and later:
        _record(incident, "level_skipped", level=level.level)
        return fire_level(incident, later[0].level, now)

    for user in new_users:
        IncidentPage.create(incident=incident, user=user, level=level.level)
        _record(incident, "paged", user=user, level=level.level)

    incident.next_escalation_at = (
        now + datetime.timedelta(minutes=later[0].delay_minutes) if later else None
    )
    incident.save()
    if not later:
        new_users += _page_admins_if_nobody_paged(incident)
    return new_users


def _page_admins_if_nobody_paged(incident):
    if incident.pages.exists():
        return []
    admins = _workspace_admins()
    for user in admins:
        IncidentPage.create(incident=incident, user=user, level=incident.current_level)
    _record(incident, "fallback_admins", user_ids=[u.id for u in admins])
    return admins


def trigger_incident(
    policy,
    channel,
    dedupe_key,
    title,
    details=None,
    source_url=None,
    message=None,
    now=None,
):
    """Open an incident and page level 1. Returns the incident.

    Returns ``None`` when another worker opened one for ``dedupe_key`` first
    (the unique index on open incidents rejects the second).
    """
    now = now or utc_now()
    try:
        with db.atomic():
            incident = Incident.create(
                policy=policy,
                channel=channel,
                dedupe_key=dedupe_key,
                title=title[:255],
                details=details,
                source_url=source_url,
                message=message,
                last_page_sent_at=now,
            )
            _record(incident, "triggered")
            levels = sorted_levels(policy)
            newly_paged = fire_level(incident, levels[0].level if levels else 1, now)
    except IntegrityError:
        return None
    _deliver_page(incident, [u.id for u in newly_paged])
    return incident


def run_escalations(now=None):
    """Escalate every triggered incident whose ``next_escalation_at`` has passed.

    Safe to run from every worker at once: each incident's step is claimed
    with a compare-and-set on ``(state, current_level)``, and the claim plus
    the new pages commit together, so a crash mid-step rolls back and the
    next tick retries. Returns ``[(incident, newly_paged_users)]``.
    """
    now = now or utc_now()
    results = []
    due = list(
        Incident.select().where(
            (Incident.state == "triggered")
            & (Incident.next_escalation_at.is_null(False))
            & (Incident.next_escalation_at <= now)
            & _paging_enabled()
        )
    )
    for candidate in due:
        try:
            escalated = _escalate(candidate, now)
        except Exception:  # pylint: disable=broad-exception-caught
            # A failure here (say a Redis blip in the bot message) must not
            # stop the other due incidents or this tick's page re-sends.
            current_app.logger.exception("escalating incident %s failed", candidate.id)
            continue
        if escalated is not None:
            results.append(escalated)
    return results


def _escalate(candidate, now):
    """One claimed escalation step; ``(incident, newly_paged)`` or ``None``."""
    with db.atomic():
        won = (
            Incident.update(next_escalation_at=None)
            .where(
                (Incident.id == candidate.id)
                & (Incident.state == "triggered")
                & (Incident.current_level == candidate.current_level)
                & (Incident.next_escalation_at <= now)
            )
            .execute()
        )
        if not won:
            return None
        incident = Incident.get_by_id(candidate.id)
        later = [
            lvl
            for lvl in sorted_levels(incident.policy)
            if lvl.level > incident.current_level
        ]
        if not later:
            return None
        _record(incident, "escalated", level=later[0].level)
        newly_paged = fire_level(incident, later[0].level, now)
    _deliver_page(incident, [u.id for u in newly_paged])
    if newly_paged:
        names = ", ".join(f"**{display_name(u)}**" for u in newly_paged)
        post_bot_message(
            incident.channel,
            f"⏫ **Escalated** · {incident.title} · not acknowledged, "
            f"now paging {names} (level {incident.current_level})",
        )
    broadcast_incident_update(incident)
    return incident, newly_paged


def resend_pages(now=None):
    """Re-send the page to everyone on each still-triggered incident.

    Claimed per incident with a compare-and-set on ``last_page_sent_at`` so
    only one worker re-sends each round. Returns the incidents re-sent.
    """
    now = now or utc_now()
    cutoff = now - datetime.timedelta(seconds=PAGE_REPEAT_SECONDS - 5)
    stale = (Incident.last_page_sent_at.is_null()) | (
        Incident.last_page_sent_at <= cutoff
    )
    resent = []
    for incident in list(
        Incident.select().where(
            (Incident.state == "triggered") & stale & _paging_enabled()
        )
    ):
        won = (
            Incident.update(last_page_sent_at=now)
            .where(
                (Incident.id == incident.id) & (Incident.state == "triggered") & stale
            )
            .execute()
        )
        if won:
            _deliver_page(
                incident, [user.id for user, _ in paged_users(incident)], repeat=True
            )
            resent.append(incident)
    return resent


def acknowledge_incident(incident, user, now=None):
    """Stop paging and escalation. Returns False if it wasn't triggered."""
    now = now or utc_now()
    won = (
        Incident.update(
            state="acknowledged",
            acknowledged_by=user,
            acknowledged_at=now,
            next_escalation_at=None,
        )
        .where((Incident.id == incident.id) & (Incident.state == "triggered"))
        .execute()
    )
    if not won:
        return False
    incident = Incident.get_by_id(incident.id)
    _record(incident, "acknowledged", user=user)
    _deliver_page_cleared(incident, [u.id for u, _ in paged_users(incident)])
    broadcast_incident_update(incident)
    post_bot_message(
        incident.channel,
        f"✅ **Acknowledged** · {incident.title} · by **{display_name(user)}**",
    )
    return True


def resolve_incident(incident, user=None, now=None, announce=True):
    """Close an open incident. ``user`` None means auto-resolved (monitor UP).

    Returns the refreshed incident, or ``None`` if it was already resolved.
    """
    now = now or utc_now()
    was_triggered = incident.state == "triggered"
    won = (
        Incident.update(
            state="resolved",
            resolved_by=user,
            resolved_at=now,
            next_escalation_at=None,
        )
        .where((Incident.id == incident.id) & (Incident.state.in_(OPEN_STATES)))
        .execute()
    )
    if not won:
        return None
    incident = Incident.get_by_id(incident.id)
    _record(incident, "resolved", user=user, auto=user is None)
    if was_triggered:
        _deliver_page_cleared(incident, [u.id for u, _ in paged_users(incident)])
    broadcast_incident_update(incident)
    if announce and user is not None:
        post_bot_message(
            incident.channel,
            f"☑️ **Resolved** · {incident.title} · by **{display_name(user)}**",
        )
    return incident


def format_duration(delta):
    """Compact duration, e.g. ``45s``, ``6m``, ``1h 12m``, ``2d 3h``."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --- Uptime Kuma ---


def _monitor_target(monitor):
    """The monitored URL or host:port, or "" when there's nothing useful."""
    url = monitor.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        if url.rstrip("/") not in ("http:/", "https:/", "http://", "https://"):
            return url
    hostname = monitor.get("hostname")
    if isinstance(hostname, str) and hostname:
        port = monitor.get("port")
        return f"{hostname}:{port}" if port else hostname
    return ""


def handle_uptime_kuma_event(payload, channel, now=None):
    """Post an Uptime Kuma webhook event and open/close incidents.

    Returns a small dict describing what happened (for the webhook response
    and tests): ``{"event": "down"|"up"|..., "incident_id": …}``.
    """
    now = now or utc_now()
    heartbeat = payload.get("heartbeat")
    monitor = payload.get("monitor")
    summary = payload.get("msg")

    # "Test" button in Uptime Kuma (and any non-monitor notice): no heartbeat.
    if not isinstance(heartbeat, dict) or not isinstance(monitor, dict):
        text = summary if isinstance(summary, str) and summary else "Test notification"
        post_bot_message(channel, f"ℹ️ **Uptime Kuma** · {text}")
        return {"event": "info"}

    status = heartbeat.get("status")
    monitor_id = monitor.get("id") or heartbeat.get("monitorID")
    name = str(monitor.get("name") or f"Monitor {monitor_id}")
    target = _monitor_target(monitor)
    target_part = f" · {target}" if target else ""
    detail = heartbeat.get("msg") if isinstance(heartbeat.get("msg"), str) else ""
    dedupe_key = f"uptime-kuma:{monitor_id if monitor_id is not None else name}"

    if status == KUMA_DOWN:
        existing = open_incident_for(dedupe_key)
        if existing is not None:
            _record(existing, "repeat_down", detail=detail)
            post_bot_message(
                channel,
                f"🔴 **Still DOWN** · **{name}**{target_part}"
                + (f"\n\n{detail}" if detail else ""),
            )
            return {"event": "down", "incident_id": existing.id, "repeat": True}

        text = f"🔴 **DOWN** · **{name}**{target_part}" + (
            f"\n\n{detail}" if detail else ""
        )
        policy = EscalationPolicy.get_or_none(
            (EscalationPolicy.channel == channel) & (EscalationPolicy.is_active)
        )
        if policy is None:
            post_bot_message(channel, text)
            return {"event": "down", "incident_id": None}

        opened = {}

        def _open(message):
            opened["incident"] = trigger_incident(
                policy,
                channel,
                dedupe_key,
                title=name,
                details=detail or None,
                source_url=target or None,
                message=message,
                now=now,
            )

        post_bot_message(channel, text, on_created=_open)
        incident = opened.get("incident")
        if incident is None:
            # Lost the race to a concurrent DOWN for the same monitor: that
            # one is paging, so this is just a repeat.
            existing = open_incident_for(dedupe_key)
            if existing is not None:
                _record(existing, "repeat_down", detail=detail)
                return {"event": "down", "incident_id": existing.id, "repeat": True}
        return {"event": "down", "incident_id": incident.id if incident else None}

    if status == KUMA_UP:
        open_incidents = open_incidents_for(dedupe_key)
        resolved = [
            r
            for r in (
                resolve_incident(i, user=None, now=now, announce=False)
                for i in open_incidents
            )
            if r is not None
        ]
        suffix = ""
        if resolved:
            suffix = f" · down {format_duration(now - resolved[0].created_at)}"
            acked = next((r for r in resolved if r.acknowledged_by_id), None)
            if acked is not None:
                suffix += f" · acknowledged by {display_name(acked.acknowledged_by)}"
        post_bot_message(channel, f"🟢 **UP** · **{name}**{target_part}{suffix}")
        return {
            "event": "up",
            "incident_id": open_incidents[0].id if open_incidents else None,
        }

    labels = {
        KUMA_PENDING: ("🟡", "PENDING", "pending"),
        KUMA_MAINTENANCE: ("🔧", "MAINTENANCE", "maintenance"),
    }
    icon, label, event = labels.get(status, ("ℹ️", f"STATUS {status}", "other"))
    post_bot_message(
        channel,
        f"{icon} **{label}** · **{name}**{target_part}"
        + (f"\n\n{detail}" if detail else ""),
    )
    return {"event": event}


# --- Ticker ---


def tick(now=None):
    """One escalation + re-send pass (what each worker runs every TICK_SECONDS)."""
    now = now or utc_now()
    for label, step in (("escalation", run_escalations), ("re-send", resend_pages)):
        try:
            step(now)
        except Exception:  # pylint: disable=broad-exception-caught
            # Re-sends are the FCM backup when Redis is down, so an escalation
            # failure must not skip them.
            current_app.logger.exception("on-call %s pass failed", label)


def run_ticker(app):
    """Forever: tick every ``TICK_SECONDS`` inside a request context.

    A request context (like ``background._run_in_context``) because the bot
    messages and card updates render templates that call ``url_for``. Every
    worker runs one; the compare-and-set claims make that safe.
    """
    while True:
        time.sleep(TICK_SECONDS)
        with app.test_request_context("/"):
            try:
                tick()
            except Exception:  # pylint: disable=broad-exception-caught
                app.logger.exception("on-call ticker pass failed")
            finally:
                try:
                    if not db.is_closed():
                        db.close()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass


# --- Template helpers ---


def incident_for_message(message):
    """The incident a monitor DOWN message opened, for its in-message card.

    Only bot messages can carry one, so ordinary messages cost no query.
    """
    user = getattr(message, "user", None)
    if user is None or user.username != UPTIME_BOT_USERNAME:
        return None
    return Incident.get_or_none(Incident.message == message.id)


# --- Provisioning (init_db / tests; migrations keep their own frozen copy) ---


def ensure_uptime_channel(workspace, channel_name="devops-uptime"):
    """Create the ``uptime-bot`` user and the uptime channel. Idempotent."""
    bot, _ = User.get_or_create(
        username=UPTIME_BOT_USERNAME,
        defaults={
            "email": "uptime-bot@d8chat.local",
            "display_name": "Uptime Kuma",
            # Inactive: can never log in; only used as Message.user.
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=bot, workspace=workspace, defaults={"role": "member"}
    )
    channel, _ = Channel.get_or_create(
        workspace=workspace,
        name=channel_name,
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
        conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
    )
    ChannelMember.get_or_create(user=bot, channel=channel)
    return bot, channel


def ensure_default_oncall(channel, now=None):
    """Create the default rotation + 4-level policy for ``channel``. Idempotent."""
    now = now or utc_now()
    schedule = OnCallSchedule.get_or_none(OnCallSchedule.name == DEFAULT_SCHEDULE_NAME)
    if schedule is None:
        schedule = OnCallSchedule(
            name=DEFAULT_SCHEDULE_NAME,
            timezone=DEFAULT_TIMEZONE,
            handoff_weekday=0,
            handoff_time="08:00",
            anchor_date=datetime.date(2000, 1, 1),
        )
        schedule.anchor_date = shift_start_local(schedule, now).date()
        schedule.save()

    policy = EscalationPolicy.get_or_none(EscalationPolicy.channel == channel)
    if policy is None:
        policy = EscalationPolicy.create(
            name=DEFAULT_POLICY_NAME,
            channel=channel,
            no_oncall_start_level=DEFAULT_NO_ONCALL_START_LEVEL,
        )
        EscalationLevel.create(
            policy=policy,
            level=1,
            delay_minutes=0,
            target_type="schedule",
            target_schedule=schedule,
        )
        for level, group in DEFAULT_GROUP_LEVELS:
            EscalationLevel.create(
                policy=policy,
                level=level,
                delay_minutes=DEFAULT_ESCALATION_MINUTES,
                target_type="group",
                target_group=group,
            )
    return schedule, policy


def default_workspace():
    """The ``DevOcho`` workspace, else the oldest one."""
    workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
    if workspace is None:
        workspace = Workspace.select().order_by(Workspace.id).first()
    return workspace
