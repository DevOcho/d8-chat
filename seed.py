#!/usr/bin/env python3

from app import create_app
from app.groups import set_local_groups
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    OnCallSchedule,
    OnCallScheduleMember,
    User,
    Workspace,
    WorkspaceMember,
    db,
)
from app.services import oncall_service

# Dev on-call cast: (username, display name, password, local groups, in rotation).
# kp is the CEO (last escalation level). robert is also an on-call admin
# (oncall-admin: the on-call admin pages without being a workspace admin). In
# production these groups come from Authentik via the SSO ``groups`` claim.
ONCALL_DEV_USERS = (
    ("engineer", "DevOps Engineer", "engineer555", [], True),
    ("robert", "Robert", "robert555", ["devops-lead", "oncall-admin"], True),
    ("emilio", "Emilio", "emilio555", ["director"], False),
)


def seed_data():
    """Set up minimal dev users: update admin password, create kp user."""
    workspace, _ = Workspace.get_or_create(name="DevOcho")

    # Update admin password (created by init_db.py with a random password)
    admin = User.get_or_none(User.username == "admin")
    if admin:
        admin.set_password("d8_admin")
        admin.save()
        print("Updated admin password.")
    else:
        print("WARNING: admin user not found — run `auto init` first.")

    # Get default channels
    general = Channel.get_or_none(
        Channel.name == "general", Channel.workspace == workspace
    )
    announcements = Channel.get_or_none(
        Channel.name == "announcements", Channel.workspace == workspace
    )

    # Create kp user
    kp, created = User.get_or_create(
        username="kp",
        defaults={"email": "kp@example.com", "display_name": "KP", "is_active": True},
    )
    if created:
        kp.set_password("kp555")
        kp.save()
        print("Created user 'kp'.")

        WorkspaceMember.get_or_create(
            user=kp, workspace=workspace, defaults={"role": "member"}
        )

        for channel in [general, announcements]:
            if channel:
                ChannelMember.get_or_create(user=kp, channel=channel)
                Conversation.get_or_create(
                    conversation_id_str=f"channel_{channel.id}",
                    defaults={"type": "channel"},
                )
    else:
        print("User 'kp' already exists.")

    seed_oncall(workspace, kp)
    seed_lead_paging(workspace, kp)
    seed_interview_paging(workspace, kp)

    print("Seeding complete.")


def seed_oncall(workspace, kp):
    """Dev users for the on-call chain, their groups, and the rotation."""
    uptime = Channel.get_or_none(
        Channel.name == "devops-uptime", Channel.workspace == workspace
    )
    channels = list(
        Channel.select().where(
            (Channel.workspace == workspace)
            & (Channel.name.in_(["general", "announcements", "devops-uptime"]))
        )
    )
    schedule = OnCallSchedule.get_or_none(
        OnCallSchedule.name == oncall_service.DEFAULT_SCHEDULE_NAME
    )

    set_local_groups(kp, ["ceo"])
    if uptime:
        ChannelMember.get_or_create(user=kp, channel=uptime)

    for username, display_name, password, groups, in_rotation in ONCALL_DEV_USERS:
        user, created = User.get_or_create(
            username=username,
            defaults={
                "email": f"{username}@example.com",
                "display_name": display_name,
                "is_active": True,
            },
        )
        if created:
            user.set_password(password)
            user.save()
            print(f"Created user '{username}' (password {password}).")
        WorkspaceMember.get_or_create(
            user=user, workspace=workspace, defaults={"role": "member"}
        )
        set_local_groups(user, groups)
        for channel in channels:
            ChannelMember.get_or_create(user=user, channel=channel)
        if schedule and in_rotation:
            position = schedule.members.count()
            OnCallScheduleMember.get_or_create(
                schedule=schedule, user=user, defaults={"position": position}
            )

    if schedule:
        first = schedule.members.order_by(OnCallScheduleMember.position).first()
        oncall_service.reanchor_schedule(schedule, first.user_id if first else None)
        on_call = oncall_service.oncall_user(schedule, oncall_service.utc_now())
        print(f"On call now: {oncall_service.display_name(on_call)}.")


def seed_lead_paging(workspace, kp):
    """An SDR for the "Website leads" policy, and the leads channel for her and
    kp (the CEO, its second level)."""
    leads = Channel.get_or_none(
        Channel.name == "website-leads", Channel.workspace == workspace
    )
    sara, created = User.get_or_create(
        username="sara",
        defaults={
            "email": "sara@example.com",
            "display_name": "Sara SDR",
            "is_active": True,
        },
    )
    if created:
        sara.set_password("sara555")
        sara.save()
        print("Created user 'sara' (password sara555, group sdr).")
    WorkspaceMember.get_or_create(
        user=sara, workspace=workspace, defaults={"role": "member"}
    )
    set_local_groups(sara, ["sdr"])
    if leads:
        for user in (sara, kp):
            ChannelMember.get_or_create(user=user, channel=leads)


# The interview day's people, by the emails the local interview platform and
# website use for them (the platform's staff are <username>@example.com, the
# website's HR login is hr@devocho.com), so a page request naming them finds
# them here. (username, display name, email, password, groups).
INTERVIEW_DAY_USERS = (
    ("grader1", "Grader One", "grader1@example.com", "grader1", []),
    ("grader2", "Grader Two", "grader2@example.com", "grader2", []),
    ("hr", "HR Coordinator", "hr@devocho.com", "hr", ["hr"]),
    ("em", "Engineering Manager", "em@example.com", "em", ["engineering-manager"]),
)


def seed_interview_paging(workspace, kp):
    """The graders, HR and an engineering manager for the "Interview day"
    policy, in its private channel with kp."""
    channel = Channel.get_or_none(
        Channel.name == "interview-day", Channel.workspace == workspace
    )
    members = [kp]
    for username, display_name, email, password, groups in INTERVIEW_DAY_USERS:
        user, created = User.get_or_create(
            username=username,
            defaults={"email": email, "display_name": display_name, "is_active": True},
        )
        if created:
            user.set_password(password)
            user.save()
            print(f"Created user '{username}' (password {password}).")
        WorkspaceMember.get_or_create(
            user=user, workspace=workspace, defaults={"role": "member"}
        )
        set_local_groups(user, groups)
        members.append(user)
    if channel:
        for user in members:
            ChannelMember.get_or_create(user=user, channel=channel)


if __name__ == "__main__":
    app = create_app(start_listener=False)
    with app.app_context():
        with db.atomic():
            seed_data()
