# app/blueprints/admin_oncall.py
"""Admin UI for on-call: rotations, overrides, escalation policies, incidents.

Routes hang off ``admin_bp`` (so they share ``/admin`` and the admin layout)
but live here to keep ``admin.py`` focused on users and channels. Imported by
``register_blueprints`` before ``admin_bp`` is registered.

Form posts follow the edit-channel pattern: POST → flash → redirect back to
the page. Times are entered and shown in the schedule's timezone and stored
as naive UTC.
"""

import datetime
import functools
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import flash, g, redirect, render_template, request, url_for
from peewee import IntegrityError, fn

from ..audit import audit
from ..models import (
    Channel,
    EscalationLevel,
    EscalationPolicy,
    Incident,
    OnCallOverride,
    OnCallSchedule,
    OnCallScheduleMember,
    User,
    UserGroup,
    WorkspaceMember,
    db,
    utc_now,
)
from ..services import oncall_service
from .admin import admin_bp


def oncall_admin_required(view):
    """Like ``admin_required``, but also lets in on-call admins (members of the
    ONCALL_ADMIN_GROUP group), who get these pages and nothing else."""

    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.index"))
        if not oncall_service.can_manage_oncall(g.user):
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("main.chat_interface"))
        return view(**kwargs)

    return wrapped_view


def _active_users():
    """Real (login-capable) users for pickers — excludes the bot accounts."""
    # Subquery rather than JOIN + DISTINCT: Postgres rejects DISTINCT with an
    # ORDER BY expression that isn't in the select list.
    return list(
        User.select()
        .where(
            (User.is_active)
            & (User.id.in_(WorkspaceMember.select(WorkspaceMember.user)))
        )
        .order_by(fn.LOWER(fn.COALESCE(User.display_name, User.username)))
    )


def _group_directory():
    """``{group_name: [users]}`` for every group any user belongs to."""
    directory = {}
    rows = (
        UserGroup.select(UserGroup, User)
        .join(User)
        .where(User.is_active)
        .order_by(UserGroup.name, User.username)
    )
    for row in rows:
        directory.setdefault(row.name, []).append(row.user)
    return directory


def _parse_local(schedule, value):
    """``datetime-local`` input (schedule timezone) → naive UTC, or None."""
    try:
        local = datetime.datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return oncall_service.to_utc(schedule, local.replace(tzinfo=None))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Overview ---


@admin_bp.route("/oncall")
@oncall_admin_required
def oncall_overview():
    """Who's on call, the policies, recent incidents, and the test pager."""
    now = utc_now()
    schedules = []
    for schedule in OnCallSchedule.select().order_by(OnCallSchedule.name):
        shift_start, shift_end = oncall_service.shift_bounds(schedule, now)
        schedules.append(
            {
                "schedule": schedule,
                "on_call": oncall_service.oncall_user(schedule, now),
                "override": oncall_service.active_override(schedule, now),
                "shift_end_local": oncall_service.to_local(schedule, shift_end),
                "member_count": schedule.members.count(),
            }
        )
    policies = [
        {
            "policy": policy,
            "levels": [
                {"level": lvl, "label": oncall_service.describe_level(lvl)}
                for lvl in oncall_service.sorted_levels(policy)
            ],
        }
        for policy in EscalationPolicy.select().order_by(EscalationPolicy.name)
    ]
    incidents = list(Incident.select().order_by(Incident.id.desc()).limit(25))
    context = {
        "schedules": schedules,
        "policies": policies,
        "incidents": incidents,
        "users": _active_users(),
        "channels": list(Channel.select().order_by(Channel.name)),
    }
    if "HX-Request" in request.headers:
        return render_template("admin/oncall_content.html", **context)
    return render_template("admin/oncall.html", **context)


@admin_bp.route("/oncall/test-page", methods=["POST"])
@oncall_admin_required
def oncall_test_page():
    """Ring a user's pager with a test page."""
    user = User.get_or_none(User.id == _int_or_none(request.form.get("user_id")))
    if user is None or not user.is_active:
        flash("Pick an active user to page.", "danger")
    else:
        oncall_service.send_test_page(user)
        audit("oncall.test_page", target=user)
        flash(
            f"Test page sent to {oncall_service.display_name(user)}. It only rings "
            "where they have D8-Chat open (and on their phone once mobile paging ships).",
            "success",
        )
    return redirect(url_for("admin.oncall_overview"))


# --- Schedules ---


@admin_bp.route("/oncall/schedules", methods=["POST"])
@oncall_admin_required
def oncall_create_schedule():
    """Create an empty weekly rotation with the default handoff."""
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Schedule name is required.", "danger")
        return redirect(url_for("admin.oncall_overview"))
    schedule = OnCallSchedule(
        name=name,
        timezone=oncall_service.DEFAULT_TIMEZONE,
        handoff_weekday=0,
        handoff_time="08:00",
        anchor_date=datetime.date(2000, 1, 1),
    )
    schedule.anchor_date = oncall_service.shift_start_local(schedule, utc_now()).date()
    try:
        schedule.save()
    except IntegrityError:
        flash("A schedule with that name already exists.", "danger")
        return redirect(url_for("admin.oncall_overview"))
    audit("oncall.schedule_created", target=schedule, name=name)
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule.id))


def _schedule_or_redirect(schedule_id):
    schedule = OnCallSchedule.get_or_none(OnCallSchedule.id == schedule_id)
    if schedule is None:
        flash("Schedule not found.", "danger")
    return schedule


@admin_bp.route("/oncall/schedules/<int:schedule_id>")
@oncall_admin_required
def oncall_schedule(schedule_id):
    """Rotation, overrides, swaps and an 8-week preview for one schedule."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    now = utc_now()
    members = list(
        OnCallScheduleMember.select(OnCallScheduleMember, User)
        .join(User)
        .where(OnCallScheduleMember.schedule == schedule)
        .order_by(OnCallScheduleMember.position, OnCallScheduleMember.id)
    )
    member_ids = {m.user_id for m in members}
    overrides = list(
        OnCallOverride.select()
        .where((OnCallOverride.schedule == schedule) & (OnCallOverride.end_at > now))
        .order_by(OnCallOverride.start_at)
    )
    return render_template(
        "admin/oncall_schedule.html",
        schedule=schedule,
        members=members,
        users_to_add=[u for u in _active_users() if u.id not in member_ids],
        users=_active_users(),
        overrides=overrides,
        shifts=oncall_service.upcoming_shifts(schedule, now, weeks=8),
        swap_shifts=oncall_service.upcoming_shifts(schedule, now, weeks=12),
        on_call=oncall_service.oncall_user(schedule, now),
        to_local=lambda dt: oncall_service.to_local(schedule, dt),
        weekday_names=oncall_service.WEEKDAY_NAMES,
    )


@admin_bp.route("/oncall/schedules/<int:schedule_id>/settings", methods=["POST"])
@oncall_admin_required
def oncall_schedule_settings(schedule_id):
    """Rename, or change the timezone / handoff day / handoff time."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    back = redirect(url_for("admin.oncall_schedule", schedule_id=schedule.id))

    name = (request.form.get("name") or "").strip()
    timezone = (request.form.get("timezone") or "").strip()
    weekday = _int_or_none(request.form.get("handoff_weekday"))
    handoff_time = (request.form.get("handoff_time") or "").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        flash(
            f"Unknown timezone '{timezone}'. Use an IANA name like America/Santo_Domingo.",
            "danger",
        )
        return back
    if (
        not name
        or weekday not in range(7)
        or not oncall_service.HANDOFF_TIME_RE.match(handoff_time)
    ):
        flash(
            "Name, handoff day, and a 24-hour HH:MM handoff time are required.",
            "danger",
        )
        return back

    keep = oncall_service.rotation_user(schedule, utc_now())
    schedule.name = name
    schedule.timezone = timezone
    schedule.handoff_weekday = weekday
    schedule.handoff_time = handoff_time
    try:
        with db.atomic():
            schedule.save()
            oncall_service.reanchor_schedule(schedule, keep.id if keep else None)
    except IntegrityError:
        flash("A schedule with that name already exists.", "danger")
        return back
    audit(
        "oncall.schedule_updated",
        target=schedule,
        timezone=timezone,
        handoff_weekday=weekday,
        handoff_time=handoff_time,
    )
    flash("Schedule updated.", "success")
    return back


@admin_bp.route("/oncall/schedules/<int:schedule_id>/delete", methods=["POST"])
@oncall_admin_required
def oncall_delete_schedule(schedule_id):
    """Delete a schedule (levels that targeted it are left without a target)."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is not None:
        audit("oncall.schedule_deleted", target=schedule, name=schedule.name)
        # recursive: drops members/overrides and nulls levels' target_schedule
        # (SQLite in tests doesn't enforce the FK ON DELETE clauses).
        schedule.delete_instance(recursive=True)
        flash("Schedule deleted.", "success")
    return redirect(url_for("admin.oncall_overview"))


@admin_bp.route("/oncall/schedules/<int:schedule_id>/members", methods=["POST"])
@oncall_admin_required
def oncall_add_member(schedule_id):
    """Add an engineer to the end of the rotation."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    user = User.get_or_none(User.id == _int_or_none(request.form.get("user_id")))
    if user is None:
        flash("Pick a user to add.", "danger")
    else:
        keep = oncall_service.rotation_user(schedule, utc_now())
        next_position = (
            OnCallScheduleMember.select(fn.MAX(OnCallScheduleMember.position))
            .where(OnCallScheduleMember.schedule == schedule)
            .scalar()
        )
        try:
            with db.atomic():
                OnCallScheduleMember.create(
                    schedule=schedule,
                    user=user,
                    position=(next_position + 1) if next_position is not None else 0,
                )
                oncall_service.reanchor_schedule(schedule, keep.id if keep else user.id)
            audit("oncall.member_added", target=schedule, user_id=user.id)
            flash(
                f"{oncall_service.display_name(user)} added to the rotation.", "success"
            )
        except IntegrityError:
            flash("That user is already in the rotation.", "danger")
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule_id))


@admin_bp.route(
    "/oncall/schedules/<int:schedule_id>/members/<int:member_id>/move", methods=["POST"]
)
@oncall_admin_required
def oncall_move_member(schedule_id, member_id):
    """Move a member one place up or down the rotation order."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    members = list(
        OnCallScheduleMember.select()
        .where(OnCallScheduleMember.schedule == schedule)
        .order_by(OnCallScheduleMember.position, OnCallScheduleMember.id)
    )
    index = next((i for i, m in enumerate(members) if m.id == member_id), None)
    offset = -1 if request.form.get("direction") == "up" else 1
    if index is not None and 0 <= index + offset < len(members):
        keep = oncall_service.rotation_user(schedule, utc_now())
        members[index], members[index + offset] = (
            members[index + offset],
            members[index],
        )
        with db.atomic():
            for position, member in enumerate(members):
                member.position = position
                member.save()
            oncall_service.reanchor_schedule(schedule, keep.id if keep else None)
        audit(
            "oncall.member_moved", target=schedule, member_id=member_id, offset=offset
        )
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule_id))


@admin_bp.route(
    "/oncall/schedules/<int:schedule_id>/members/<int:member_id>/remove",
    methods=["POST"],
)
@oncall_admin_required
def oncall_remove_member(schedule_id, member_id):
    """Take an engineer out of the rotation."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    member = OnCallScheduleMember.get_or_none(
        (OnCallScheduleMember.id == member_id)
        & (OnCallScheduleMember.schedule == schedule)
    )
    if member is not None:
        keep = oncall_service.rotation_user(schedule, utc_now())
        with db.atomic():
            member.delete_instance()
            oncall_service.renumber_members(schedule)
            oncall_service.reanchor_schedule(schedule, keep.id if keep else None)
        audit("oncall.member_removed", target=schedule, user_id=member.user_id)
        flash("Removed from the rotation.", "success")
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule_id))


@admin_bp.route("/oncall/schedules/<int:schedule_id>/overrides", methods=["POST"])
@oncall_admin_required
def oncall_add_override(schedule_id):
    """Cover or time off: someone (or nobody) replaces the rotation for a window."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    back = redirect(url_for("admin.oncall_schedule", schedule_id=schedule.id))

    start_at = _parse_local(schedule, request.form.get("start"))
    end_at = _parse_local(schedule, request.form.get("end"))
    if start_at is None or end_at is None or end_at <= start_at:
        flash("Enter a start and an end, with the end after the start.", "danger")
        return back
    user_id = _int_or_none(request.form.get("user_id"))
    user = User.get_or_none(User.id == user_id) if user_id else None
    if user_id and user is None:
        flash("That user doesn't exist.", "danger")
        return back

    override = OnCallOverride.create(
        schedule=schedule,
        user=user,
        start_at=start_at,
        end_at=end_at,
        reason=(request.form.get("reason") or "").strip()[:255] or None,
        created_by=g.user,
    )
    audit(
        "oncall.override_created",
        target=schedule,
        override_id=override.id,
        user_id=user.id if user else None,
        start=start_at.isoformat(),
        end=end_at.isoformat(),
    )
    who = (
        oncall_service.display_name(user)
        if user
        else "nobody (pages go to the no-on-call level)"
    )
    flash(f"Override added: {who}.", "success")
    return back


@admin_bp.route(
    "/oncall/schedules/<int:schedule_id>/overrides/<int:override_id>/delete",
    methods=["POST"],
)
@oncall_admin_required
def oncall_delete_override(schedule_id, override_id):
    """Remove an override; the rotation applies again for that window."""
    override = OnCallOverride.get_or_none(
        (OnCallOverride.id == override_id) & (OnCallOverride.schedule == schedule_id)
    )
    if override is not None:
        audit(
            "oncall.override_deleted",
            target=("oncallschedule", schedule_id),
            override_id=override_id,
        )
        override.delete_instance()
        flash("Override removed.", "success")
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule_id))


@admin_bp.route("/oncall/schedules/<int:schedule_id>/swap", methods=["POST"])
@oncall_admin_required
def oncall_swap(schedule_id):
    """Swap two upcoming shifts (two full-week overrides)."""
    schedule = _schedule_or_redirect(schedule_id)
    if schedule is None:
        return redirect(url_for("admin.oncall_overview"))
    try:
        start_a = datetime.datetime.fromisoformat(request.form.get("shift_a", ""))
        start_b = datetime.datetime.fromisoformat(request.form.get("shift_b", ""))
    except ValueError:
        start_a = start_b = None
    result = (
        oncall_service.swap_shifts(schedule, start_a, start_b, created_by=g.user)
        if start_a and start_b
        else None
    )
    if result is None:
        flash(
            "Pick two different upcoming weeks that belong to two different people.",
            "danger",
        )
    else:
        audit(
            "oncall.shifts_swapped",
            target=schedule,
            shift_a=start_a.isoformat(),
            shift_b=start_b.isoformat(),
        )
        flash("Weeks swapped.", "success")
    return redirect(url_for("admin.oncall_schedule", schedule_id=schedule_id))


# --- Escalation policies ---


@admin_bp.route("/oncall/policies", methods=["POST"])
@oncall_admin_required
def oncall_create_policy():
    """Create an (empty) escalation policy for a channel."""
    name = (request.form.get("name") or "").strip()
    channel = Channel.get_or_none(
        Channel.id == _int_or_none(request.form.get("channel_id"))
    )
    if not name or channel is None:
        flash("A name and a channel are required.", "danger")
        return redirect(url_for("admin.oncall_overview"))
    try:
        policy = EscalationPolicy.create(name=name, channel=channel)
    except IntegrityError:
        flash(f"#{channel.name} already has an escalation policy.", "danger")
        return redirect(url_for("admin.oncall_overview"))
    audit("oncall.policy_created", target=policy, channel_id=channel.id)
    return redirect(url_for("admin.oncall_policy", policy_id=policy.id))


def _policy_or_redirect(policy_id):
    policy = EscalationPolicy.get_or_none(EscalationPolicy.id == policy_id)
    if policy is None:
        flash("Escalation policy not found.", "danger")
    return policy


@admin_bp.route("/oncall/policies/<int:policy_id>")
@oncall_admin_required
def oncall_policy(policy_id):
    """Edit a policy's levels and settings."""
    policy = _policy_or_redirect(policy_id)
    if policy is None:
        return redirect(url_for("admin.oncall_overview"))
    now = utc_now()
    levels = [
        {
            "level": lvl,
            "label": oncall_service.describe_level(lvl),
            "targets": oncall_service.level_targets(lvl, now),
        }
        for lvl in oncall_service.sorted_levels(policy)
    ]
    return render_template(
        "admin/oncall_policy.html",
        policy=policy,
        levels=levels,
        schedules=list(OnCallSchedule.select().order_by(OnCallSchedule.name)),
        users=_active_users(),
        groups=_group_directory(),
        next_level=(max((lv["level"].level for lv in levels), default=0) + 1),
    )


@admin_bp.route("/oncall/policies/<int:policy_id>/settings", methods=["POST"])
@oncall_admin_required
def oncall_policy_settings(policy_id):
    """Rename, toggle paging, or change the no-on-call start level."""
    policy = _policy_or_redirect(policy_id)
    if policy is None:
        return redirect(url_for("admin.oncall_overview"))
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name is required.", "danger")
    else:
        was_active = policy.is_active
        policy.name = name
        policy.is_active = request.form.get("is_active") == "on"
        policy.no_oncall_start_level = _int_or_none(
            request.form.get("no_oncall_start_level")
        )
        policy.save()
        if was_active and not policy.is_active:
            oncall_service.silence_policy(policy)
        audit(
            "oncall.policy_updated",
            target=policy,
            is_active=policy.is_active,
            no_oncall_start_level=policy.no_oncall_start_level,
        )
        flash("Policy updated.", "success")
    return redirect(url_for("admin.oncall_policy", policy_id=policy_id))


def _apply_level_form(level):
    """Copy the level form onto ``level``; returns an error string or None."""
    number = _int_or_none(request.form.get("level"))
    delay = _int_or_none(request.form.get("delay_minutes"))
    target_type = request.form.get("target_type")
    if number is None or number < 1 or delay is None or delay < 0:
        return "Level must be 1 or more and the delay 0 or more minutes."
    if target_type not in oncall_service.TARGET_TYPES:
        return "Pick a target type."

    level.level = number
    level.delay_minutes = delay
    level.target_type = target_type
    level.target_schedule = None
    level.target_group = None
    level.target_user = None
    if target_type == "schedule":
        schedule = OnCallSchedule.get_or_none(
            OnCallSchedule.id == _int_or_none(request.form.get("target_schedule_id"))
        )
        if schedule is None:
            return "Pick a schedule."
        level.target_schedule = schedule
    elif target_type == "group":
        group = (request.form.get("target_group") or "").strip().lower()
        if not group:
            return "Enter a group name."
        level.target_group = group
    else:
        user = User.get_or_none(
            User.id == _int_or_none(request.form.get("target_user_id"))
        )
        if user is None:
            return "Pick a user."
        level.target_user = user
    return None


@admin_bp.route("/oncall/policies/<int:policy_id>/levels", methods=["POST"])
@oncall_admin_required
def oncall_add_level(policy_id):
    """Append an escalation level."""
    policy = _policy_or_redirect(policy_id)
    if policy is None:
        return redirect(url_for("admin.oncall_overview"))
    level = EscalationLevel(policy=policy)
    error = _apply_level_form(level)
    if error:
        flash(error, "danger")
    else:
        try:
            level.save()
            audit("oncall.level_added", target=policy, level=level.level)
            flash(f"Level {level.level} added.", "success")
        except IntegrityError:
            flash(f"Level {level.level} already exists.", "danger")
    return redirect(url_for("admin.oncall_policy", policy_id=policy_id))


@admin_bp.route(
    "/oncall/policies/<int:policy_id>/levels/<int:level_id>", methods=["POST"]
)
@oncall_admin_required
def oncall_update_level(policy_id, level_id):
    """Change a level's number, delay or target."""
    level = EscalationLevel.get_or_none(
        (EscalationLevel.id == level_id) & (EscalationLevel.policy == policy_id)
    )
    if level is None:
        flash("Level not found.", "danger")
        return redirect(url_for("admin.oncall_policy", policy_id=policy_id))
    error = _apply_level_form(level)
    if error:
        flash(error, "danger")
    else:
        try:
            level.save()
            audit(
                "oncall.level_updated",
                target=("escalationpolicy", policy_id),
                level=level.level,
            )
            flash(f"Level {level.level} saved.", "success")
        except IntegrityError:
            flash(f"Level {level.level} already exists.", "danger")
    return redirect(url_for("admin.oncall_policy", policy_id=policy_id))


@admin_bp.route(
    "/oncall/policies/<int:policy_id>/levels/<int:level_id>/delete", methods=["POST"]
)
@oncall_admin_required
def oncall_delete_level(policy_id, level_id):
    """Remove a level."""
    level = EscalationLevel.get_or_none(
        (EscalationLevel.id == level_id) & (EscalationLevel.policy == policy_id)
    )
    if level is not None:
        audit(
            "oncall.level_deleted",
            target=("escalationpolicy", policy_id),
            level=level.level,
        )
        level.delete_instance()
        flash("Level removed.", "success")
    return redirect(url_for("admin.oncall_policy", policy_id=policy_id))


# --- Incidents ---


@admin_bp.route("/oncall/incidents/<int:incident_id>")
@oncall_admin_required
def oncall_incident(incident_id):
    """Incident timeline."""
    incident = Incident.get_or_none(Incident.id == incident_id)
    if incident is None:
        flash("Incident not found.", "danger")
        return redirect(url_for("admin.oncall_overview"))
    return render_template(
        "admin/oncall_incident.html",
        incident=incident,
        events=oncall_service.incident_events(incident),
        paged=oncall_service.paged_users(incident),
    )
