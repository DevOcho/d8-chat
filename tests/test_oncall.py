# tests/test_oncall.py
"""On-call paging: rotations, overrides, escalation, incidents, the Uptime
Kuma webhook, the web/mobile endpoints, SSO group sync and the admin UI."""

import datetime
import json
from types import SimpleNamespace

import pytest

from app.chat_manager import chat_manager
from app.groups import set_local_groups, sync_sso_groups, users_in_group
from app.models import (
    ChannelMember,
    EscalationLevel,
    Incident,
    IncidentEvent,
    IncidentPage,
    Message,
    OnCallOverride,
    OnCallSchedule,
    OnCallScheduleMember,
    User,
    UserGroup,
    Workspace,
    WorkspaceMember,
)
from app.services import lead_response, oncall_service

# Wednesday 2026-10-14 11:00 in Santo Domingo (UTC-4, no DST). The current
# shift started Monday 2026-10-12 08:00 local = 12:00 UTC.
NOW = datetime.datetime(2026, 10, 14, 15, 0)
SHIFT_START = datetime.datetime(2026, 10, 12, 12, 0)
MINUTE = datetime.timedelta(minutes=1)
DAY = datetime.timedelta(days=1)
WEEK = datetime.timedelta(days=7)
KUMA_HEADERS = {"Authorization": "Bearer test-uptime-kuma-key"}


@pytest.fixture(autouse=True)
def frozen_now(mocker):
    """Pin the service's clock so webhook-driven paths see the fixture's week."""
    mocker.patch.object(oncall_service, "utc_now", return_value=NOW)


def _user(username, groups=(), admin=False):
    user = User.create(
        username=username,
        email=f"{username}@example.com",
        display_name=username.title(),
    )
    workspace = Workspace.get(name="DevOcho")
    WorkspaceMember.create(
        user=user, workspace=workspace, role="admin" if admin else "member"
    )
    set_local_groups(user, groups)
    return user


@pytest.fixture
def oncall(test_db):
    """#devops-uptime + default policy, a 2-person rotation (eng on call now),
    and lead / director / ceo in their groups."""
    workspace = Workspace.get(name="DevOcho")
    bot, channel = oncall_service.ensure_uptime_channel(workspace)
    schedule, policy = oncall_service.ensure_default_oncall(channel, now=NOW)
    eng = _user("eng")
    lead = _user("lead", ["devops-lead"])
    director = _user("director", ["director"])
    ceo = _user("ceo", ["ceo"])
    for position, user in enumerate((eng, lead)):
        OnCallScheduleMember.create(schedule=schedule, user=user, position=position)
    oncall_service.reanchor_schedule(schedule, eng.id, now=NOW)
    return SimpleNamespace(
        bot=bot,
        channel=channel,
        schedule=OnCallSchedule.get_by_id(schedule.id),
        policy=policy,
        eng=eng,
        lead=lead,
        director=director,
        ceo=ceo,
    )


def _frames(user_id, frame_type):
    """WS frames published to ``user:<id>`` of the given type."""
    frames = []
    for call in chat_manager.redis_client.publish.call_args_list:
        channel, payload = call.args
        if channel != f"user:{user_id}":
            continue
        data = json.loads(payload)
        if data.get("type") == frame_type:
            frames.append(data)
    return frames


def _paged_ids(incident):
    return {
        page.user_id
        for page in IncidentPage.select().where(IncidentPage.incident == incident)
    }


def _kuma(status, monitor_id=12, name="Acme site", msg="timeout"):
    return {
        "heartbeat": {"monitorID": monitor_id, "status": status, "msg": msg},
        "monitor": {
            "id": monitor_id,
            "name": name,
            "url": "https://acme.example",
            "type": "http",
        },
        "msg": f"[{name}] status {status}",
    }


def _login(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return client


# --- Rotation math ---


def test_shift_bounds_follow_monday_0800_atlantic(oncall):
    start, end = oncall_service.shift_bounds(oncall.schedule, NOW)
    assert start == SHIFT_START
    assert end == SHIFT_START + WEEK
    # One minute before the handoff still belongs to the previous shift.
    assert oncall_service.shift_bounds(oncall.schedule, SHIFT_START - MINUTE)[0] == (
        SHIFT_START - WEEK
    )


def test_rotation_alternates_weekly(oncall):
    sched = oncall.schedule
    assert oncall_service.oncall_user(sched, NOW) == oncall.eng
    assert oncall_service.oncall_user(sched, NOW + WEEK) == oncall.lead
    assert oncall_service.oncall_user(sched, NOW + 2 * WEEK) == oncall.eng
    assert oncall_service.oncall_user(sched, NOW - WEEK) == oncall.lead


def test_dst_timezone_keeps_local_handoff_hour(oncall):
    sched = oncall.schedule
    sched.timezone = "America/New_York"
    sched.save()
    # Shift containing Wed Oct 28 runs Mon Oct 26 08:00 EDT → Mon Nov 2 08:00 EST.
    start, end = oncall_service.shift_bounds(sched, datetime.datetime(2026, 10, 28, 12))
    assert start == datetime.datetime(2026, 10, 26, 12, 0)
    assert end == datetime.datetime(2026, 11, 2, 13, 0)


def test_override_cover_and_no_cover(oncall):
    sched = oncall.schedule
    OnCallOverride.create(
        schedule=sched, user=oncall.director, start_at=NOW - MINUTE, end_at=NOW + MINUTE
    )
    assert oncall_service.oncall_user(sched, NOW) == oncall.director
    # A newer "nobody" override on top wins.
    OnCallOverride.create(
        schedule=sched, user=None, start_at=NOW - MINUTE, end_at=NOW + MINUTE
    )
    assert oncall_service.oncall_user(sched, NOW) is None
    # Outside the window the rotation applies again.
    assert oncall_service.oncall_user(sched, NOW + 2 * MINUTE) == oncall.eng


def test_inactive_rotation_member_counts_as_nobody(oncall):
    oncall.eng.is_active = False
    oncall.eng.save()
    assert oncall_service.oncall_user(oncall.schedule, NOW) is None


def test_reanchor_keeps_current_person_when_roster_changes(oncall):
    sched = oncall.schedule
    newbie = _user("newbie")
    OnCallScheduleMember.create(schedule=sched, user=newbie, position=-1)  # now first
    oncall_service.renumber_members(sched)
    oncall_service.reanchor_schedule(sched, oncall.eng.id, now=NOW)
    sched = OnCallSchedule.get_by_id(sched.id)
    assert oncall_service.oncall_user(sched, NOW) == oncall.eng
    # Order newbie → eng → lead continues from eng.
    assert oncall_service.oncall_user(sched, NOW + WEEK) == oncall.lead
    assert oncall_service.oncall_user(sched, NOW + 2 * WEEK) == newbie


def test_swap_shifts_creates_two_full_week_overrides(oncall):
    sched = oncall.schedule
    result = oncall_service.swap_shifts(sched, SHIFT_START, SHIFT_START + WEEK, now=NOW)
    assert result is not None
    assert oncall_service.oncall_user(sched, NOW) == oncall.lead
    assert oncall_service.oncall_user(sched, NOW + WEEK) == oncall.eng
    # Same person on both weeks → refused.
    assert (
        oncall_service.swap_shifts(sched, SHIFT_START, SHIFT_START + 2 * WEEK, now=NOW)
        is None
    )


def test_upcoming_shifts_lists_overrides(oncall):
    OnCallOverride.create(
        schedule=oncall.schedule,
        user=oncall.director,
        start_at=SHIFT_START + WEEK,
        end_at=SHIFT_START + 2 * WEEK,
    )
    shifts = oncall_service.upcoming_shifts(oncall.schedule, NOW, weeks=3)
    assert [s["rotation_user"] for s in shifts] == [oncall.eng, oncall.lead, oncall.eng]
    assert shifts[0]["is_current"] and not shifts[1]["is_current"]
    assert [len(s["overrides"]) for s in shifts] == [0, 1, 0]


# --- Groups ---


def test_sync_sso_groups_mirrors_claim_and_keeps_local(test_db):
    user = _user("someone", ["director"])  # local
    sync_sso_groups(user, ["DevOps-Lead", "Engineers"])
    names = {
        (g.name, g.source) for g in UserGroup.select().where(UserGroup.user == user)
    }
    assert names == {
        ("director", "local"),
        ("devops-lead", "sso"),
        ("engineers", "sso"),
    }

    sync_sso_groups(user, None)  # claim missing → untouched
    assert UserGroup.select().where(UserGroup.user == user).count() == 3

    sync_sso_groups(user, [])  # claim present but empty → SSO groups removed
    names = {
        (g.name, g.source) for g in UserGroup.select().where(UserGroup.user == user)
    }
    assert names == {("director", "local")}


def test_users_in_group_skips_inactive(test_db):
    active = _user("a1", ["ceo"])
    inactive = _user("a2", ["ceo"])
    inactive.is_active = False
    inactive.save()
    assert users_in_group("CEO") == [active]


# --- Escalation ---


def _trigger(oncall, now=NOW):
    return oncall_service.trigger_incident(
        oncall.policy, oncall.channel, "uptime-kuma:12", "Acme site", now=now
    )


def test_trigger_pages_oncall_engineer_first(oncall):
    incident = _trigger(oncall)
    assert _paged_ids(incident) == {oncall.eng.id}
    assert incident.current_level == 1
    assert incident.next_escalation_at == NOW + 10 * MINUTE
    frames = _frames(oncall.eng.id, "page")
    assert frames and frames[0]["incident"]["id"] == incident.id
    assert not _frames(oncall.lead.id, "page")


def test_escalation_is_cumulative_through_ceo(oncall):
    incident = _trigger(oncall)
    assert oncall_service.run_escalations(NOW + 9 * MINUTE) == []

    oncall_service.run_escalations(NOW + 10 * MINUTE)
    incident = Incident.get_by_id(incident.id)
    assert incident.current_level == 2
    assert _paged_ids(incident) == {oncall.eng.id, oncall.lead.id}

    oncall_service.run_escalations(NOW + 20 * MINUTE)
    oncall_service.run_escalations(NOW + 30 * MINUTE)
    incident = Incident.get_by_id(incident.id)
    assert incident.current_level == 4
    assert incident.next_escalation_at is None
    assert _paged_ids(incident) == {
        oncall.eng.id,
        oncall.lead.id,
        oncall.director.id,
        oncall.ceo.id,
    }
    # Escalations are announced in the channel.
    texts = [m.content for m in Message.select().where(Message.user == oncall.bot)]
    assert sum("Escalated" in t for t in texts) == 3


def test_escalation_step_is_claimed_once(oncall):
    _trigger(oncall)
    first = oncall_service.run_escalations(NOW + 10 * MINUTE)
    second = oncall_service.run_escalations(NOW + 10 * MINUTE)
    assert len(first) == 1 and second == []


def test_team_lead_on_call_skips_level_two(oncall):
    incident = _trigger(oncall, now=NOW + WEEK)  # lead's week
    assert _paged_ids(incident) == {oncall.lead.id}
    oncall_service.run_escalations(NOW + WEEK + 10 * MINUTE)
    incident = Incident.get_by_id(incident.id)
    # L2 (the lead again) adds nobody, so the director is paged right away.
    assert incident.current_level == 3
    assert _paged_ids(incident) == {oncall.lead.id, oncall.director.id}
    assert IncidentEvent.select().where(IncidentEvent.kind == "level_skipped").exists()


def test_nobody_on_call_pages_director_then_ceo(oncall):
    OnCallOverride.create(
        schedule=oncall.schedule,
        user=None,
        start_at=SHIFT_START,
        end_at=SHIFT_START + WEEK,
    )
    incident = _trigger(oncall)
    assert incident.current_level == 3
    assert _paged_ids(incident) == {oncall.director.id}
    oncall_service.run_escalations(NOW + 10 * MINUTE)
    assert _paged_ids(incident) == {oncall.director.id, oncall.ceo.id}


def test_empty_chain_falls_back_to_workspace_admins(oncall):
    EscalationLevel.delete().execute()
    admin = _user("boss", admin=True)
    incident = _trigger(oncall)
    assert _paged_ids(incident) == {admin.id}


def test_acknowledge_stops_paging_and_escalation(oncall):
    incident = _trigger(oncall)
    assert oncall_service.acknowledge_incident(incident, oncall.eng, now=NOW + MINUTE)
    incident = Incident.get_by_id(incident.id)
    assert incident.state == "acknowledged"
    assert incident.acknowledged_by == oncall.eng
    assert _frames(oncall.eng.id, "page_cleared")
    assert oncall_service.run_escalations(NOW + 10 * MINUTE) == []
    assert oncall_service.resend_pages(NOW + 5 * MINUTE) == []
    # A second acknowledge is a no-op.
    assert not oncall_service.acknowledge_incident(incident, oncall.lead)


def test_resend_pages_every_minute_once(oncall):
    incident = _trigger(oncall)
    assert oncall_service.resend_pages(NOW + 30 * datetime.timedelta(seconds=1)) == []
    assert oncall_service.resend_pages(NOW + MINUTE) == [incident]
    assert oncall_service.resend_pages(NOW + MINUTE) == []
    repeats = [f for f in _frames(oncall.eng.id, "page") if f["repeat"]]
    assert len(repeats) == 1


def test_only_one_open_incident_per_monitor(oncall):
    """The DB refuses a second open incident for the same dedupe key, so a
    racing duplicate can't page everyone twice."""
    first = _trigger(oncall)
    assert _trigger(oncall) is None
    assert Incident.select().count() == 1
    assert len([f for f in _frames(oncall.eng.id, "page") if not f["repeat"]]) == 1
    # Once resolved, the monitor can open a new incident.
    oncall_service.resolve_incident(first)
    assert _trigger(oncall) is not None


def test_racing_down_webhook_becomes_a_repeat(client, oncall, mocker):
    """Both workers saw no open incident; the loser reports the winner's."""
    down = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    winner = Incident.get_by_id(down.get_json()["incident_id"])
    mocker.patch.object(oncall_service, "open_incident_for", side_effect=[None, winner])
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    assert res.get_json()["incident_id"] == winner.id
    assert res.get_json()["repeat"] is True
    assert Incident.select().count() == 1


def test_escalation_failure_skips_only_that_incident(oncall, mocker):
    """A failure after the claim (say Redis down for the bot message) must
    not stop the other due incidents."""
    a = _trigger(oncall)
    b = oncall_service.trigger_incident(
        oncall.policy, oncall.channel, "uptime-kuma:99", "Other site", now=NOW
    )
    mocker.patch.object(
        oncall_service, "post_bot_message", side_effect=RuntimeError("redis down")
    )
    assert oncall_service.run_escalations(NOW + 10 * MINUTE) == []
    for incident in (a, b):
        assert Incident.get_by_id(incident.id).current_level == 2


def test_tick_resends_pages_even_when_escalation_fails(oncall, mocker):
    mocker.patch.object(
        oncall_service, "run_escalations", side_effect=RuntimeError("boom")
    )
    resend = mocker.patch.object(oncall_service, "resend_pages")
    oncall_service.tick(NOW)
    resend.assert_called_once_with(NOW)


def test_paging_off_stops_open_incidents_and_back_on_resumes(
    admin_client, oncall, mocker
):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    incident = _trigger(oncall)
    settings_url = f"/admin/oncall/policies/{oncall.policy.id}/settings"

    admin_client.post(settings_url, data={"name": oncall.policy.name})

    # The ringing pager is silenced, and nothing re-pages or escalates.
    assert _frames(oncall.eng.id, "page_cleared")[-1]["state"] == "paused"
    assert cleared.call_args.kwargs["alert"] == "🔕 Acme site: paging switched off"
    assert oncall_service.active_pages_for(oncall.eng) == []
    assert oncall_service.resend_pages(NOW + MINUTE) == []
    assert oncall_service.run_escalations(NOW + 10 * MINUTE) == []
    assert Incident.get_by_id(incident.id).state == "triggered"

    admin_client.post(
        settings_url, data={"name": oncall.policy.name, "is_active": "on"}
    )
    assert oncall_service.active_pages_for(oncall.eng) == [incident]
    assert oncall_service.resend_pages(NOW + 2 * MINUTE) == [incident]


def test_user_in_chain(oncall):
    incident = _trigger(oncall)
    outsider = _user("outsider")
    for user in (oncall.eng, oncall.lead, oncall.director, oncall.ceo):
        assert oncall_service.user_in_chain(incident, user)
    assert not oncall_service.user_in_chain(incident, outsider)


def test_can_be_paged(oncall):
    """Who gets the full pager setup in the mobile app."""
    for user in (oncall.eng, oncall.lead, oncall.director, oncall.ceo):
        assert oncall_service.can_be_paged(user)
    assert not oncall_service.can_be_paged(_user("outsider"))
    assert not oncall_service.can_be_paged(None)
    # Workspace admins are paged when nobody else can be.
    assert oncall_service.can_be_paged(_user("boss", admin=True))
    # On-call admins only manage the rotation; nobody pages them.
    assert not oncall_service.can_be_paged(_user("ops", ["oncall-admin"]))


def test_can_be_paged_follows_overrides_and_the_rotation(oncall):
    cover = _user("cover")
    override = OnCallOverride.create(
        schedule=oncall.schedule,
        user=cover,
        start_at=NOW + DAY,
        end_at=NOW + 2 * DAY,
    )
    assert oncall_service.can_be_paged(cover)
    # A cover that's over doesn't make anyone pageable.
    assert not oncall_service.can_be_paged(cover, now=override.end_at)

    OnCallScheduleMember.delete().where(
        OnCallScheduleMember.user == oncall.eng
    ).execute()
    assert not oncall_service.can_be_paged(oncall.eng)


def test_can_be_paged_skips_deactivated_users(oncall):
    oncall.lead.is_active = False
    oncall.lead.save()
    assert not oncall_service.can_be_paged(oncall.lead)


# --- Uptime Kuma webhook ---


def test_webhook_down_posts_and_pages(client, oncall):
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["event"] == "down" and data["incident_id"]
    incident = Incident.get_by_id(data["incident_id"])
    assert incident.state == "triggered"
    assert incident.message.content.startswith(
        "🔴 **DOWN** · **Acme site** · https://acme.example"
    )
    assert incident.message.user == oncall.bot
    assert _paged_ids(incident) == {oncall.eng.id}


def test_webhook_repeat_down_dedupes(client, oncall):
    first = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    second = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    assert second.get_json()["repeat"] is True
    assert second.get_json()["incident_id"] == first.get_json()["incident_id"]
    assert Incident.select().count() == 1


def test_webhook_up_resolves(client, oncall):
    down = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    up = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(1), headers=KUMA_HEADERS
    )
    assert up.get_json() == {
        "ok": True,
        "event": "up",
        "incident_id": down.get_json()["incident_id"],
    }
    incident = Incident.get_by_id(down.get_json()["incident_id"])
    assert incident.state == "resolved" and incident.resolved_by is None
    assert _frames(oncall.eng.id, "page_cleared")
    last = Message.select().order_by(Message.id.desc()).first()
    assert (
        last.content.startswith("🟢 **UP** · **Acme site**") and "down " in last.content
    )


def test_webhook_pending_and_test_notification_do_not_page(client, oncall):
    pending = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(2), headers=KUMA_HEADERS
    )
    assert pending.get_json()["event"] == "pending"
    test = client.post(
        "/api/v1/integrations/uptime-kuma",
        json={"heartbeat": None, "monitor": None, "msg": "Uptime Kuma Testing"},
        headers=KUMA_HEADERS,
    )
    assert test.get_json()["event"] == "info"
    assert Incident.select().count() == 0
    assert Message.select().where(Message.user == oncall.bot).count() == 2


def test_webhook_accepts_multipart_form(client, oncall):
    res = client.post(
        "/api/v1/integrations/uptime-kuma",
        data={"data": json.dumps(_kuma(0))},
        headers=KUMA_HEADERS,
    )
    assert res.status_code == 200 and res.get_json()["event"] == "down"


def test_webhook_without_policy_posts_but_does_not_page(client, oncall):
    oncall.policy.is_active = False
    oncall.policy.save()
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    assert res.get_json()["incident_id"] is None
    assert Incident.select().count() == 0
    assert Message.select().where(Message.user == oncall.bot).count() == 1


def test_webhook_unknown_channel_and_bad_body(client, app, oncall):
    bad = client.post(
        "/api/v1/integrations/uptime-kuma", data="nope", headers=KUMA_HEADERS
    )
    assert bad.status_code == 400
    app.config["UPTIME_KUMA_CHANNEL"] = "nope"
    missing = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    assert missing.status_code == 404


def test_channel_render_shows_incident_card(client, oncall):
    client.post("/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS)
    ChannelMember.create(user=oncall.eng, channel=oncall.channel)
    _login(client, oncall.eng)
    res = client.get(f"/chat/channel/{oncall.channel.id}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "incident-card-" in html and "Acknowledge" in html


def test_api_messages_carry_their_incident(client, oncall):
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    incident = Incident.get_by_id(res.get_json()["incident_id"])
    ordinary = Message.create(
        user=oncall.eng, conversation=incident.message.conversation, content="on it"
    )
    ChannelMember.create(user=oncall.eng, channel=oncall.channel)
    messages = client.get(
        f"/api/v1/conversations/channel_{oncall.channel.id}/messages",
        headers=_api_headers(oncall.eng),
    ).get_json()["messages"]
    by_id = {m["id"]: m for m in messages}
    down = by_id[incident.message_id]["incident"]
    assert down["id"] == incident.id and down["state"] == "triggered"
    assert down["message_id"] == incident.message_id
    assert by_id[ordinary.id]["incident"] is None


def test_live_down_message_frame_carries_its_incident(client, oncall):
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    incident = Incident.get_by_id(res.get_json()["incident_id"])
    frames = []
    for call in chat_manager.redis_client.publish.call_args_list:
        topic, payload = call.args
        api_data = json.loads(payload).get("api_data") or {}
        if topic == f"chat:channel_{oncall.channel.id}" and (
            api_data.get("type") == "new_message"
        ):
            frames.append(api_data["data"])
    down = next(f for f in frames if f["id"] == incident.message_id)
    assert down["incident"]["id"] == incident.id


# --- Web endpoints ---


def test_web_ack_by_chain_member(client, oncall):
    incident = _trigger(oncall)
    _login(client, oncall.lead)
    res = client.post(f"/oncall/incidents/{incident.id}/ack")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert Incident.get_by_id(incident.id).acknowledged_by == oncall.lead


def test_web_ack_refused_outside_chain(client, oncall):
    incident = _trigger(oncall)
    _login(client, _user("outsider"))
    res = client.post(
        f"/oncall/incidents/{incident.id}/ack", headers={"HX-Request": "true"}
    )
    assert res.status_code == 403
    assert "escalation chain" in res.get_data(as_text=True)
    assert Incident.get_by_id(incident.id).state == "triggered"


def test_web_resolve_and_active_pages(client, oncall):
    incident = _trigger(oncall)
    _login(client, oncall.eng)
    active = client.get("/oncall/pages/active").get_json()["incidents"]
    assert [i["id"] for i in active] == [incident.id]
    res = client.post(f"/oncall/incidents/{incident.id}/resolve")
    assert res.get_json()["ok"] is True
    assert Incident.get_by_id(incident.id).resolved_by == oncall.eng
    assert client.get("/oncall/pages/active").get_json()["incidents"] == []


def test_web_test_page_rings_only_me(client, oncall):
    _login(client, oncall.eng)
    assert client.post("/oncall/test-page").status_code == 204
    frames = _frames(oncall.eng.id, "page")
    assert len(frames) == 1 and frames[0]["test"] is True
    assert Incident.select().count() == 0


def test_chat_page_shows_on_call_badge(client, oncall):
    _login(client, oncall.eng)
    html = client.get("/chat").get_data(as_text=True)
    assert "You're on call" in html and 'id="pager-overlay"' in html


# --- Mobile API ---


def _api_headers(user):
    from app.blueprints.api_v1 import generate_api_token

    return {"Authorization": f"Bearer d8_sec_{generate_api_token(user.id)}"}


def test_api_active_ack_and_detail(client, oncall):
    incident = _trigger(oncall)
    headers = _api_headers(oncall.eng)
    active = client.get("/api/v1/oncall/incidents/active", headers=headers).get_json()
    assert [i["id"] for i in active["incidents"]] == [incident.id]

    res = client.post(f"/api/v1/oncall/incidents/{incident.id}/ack", headers=headers)
    assert res.status_code == 200
    assert res.get_json()["incident"]["state"] == "acknowledged"
    again = client.post(f"/api/v1/oncall/incidents/{incident.id}/ack", headers=headers)
    assert again.status_code == 409

    detail = client.get(
        f"/api/v1/oncall/incidents/{incident.id}", headers=headers
    ).get_json()
    kinds = [e["kind"] for e in detail["incident"]["events"]]
    assert kinds[:3] == ["triggered", "paged", "acknowledged"]


def test_api_ack_conflict_reports_the_current_state(client, oncall, mocker):
    """Losing an acknowledge race returns the incident as it is now."""
    incident = _trigger(oncall)
    real_ack = oncall_service.acknowledge_incident

    def lead_wins_first(inc, _user):
        real_ack(inc, oncall.lead)
        return False

    mocker.patch.object(
        oncall_service, "acknowledge_incident", side_effect=lead_wins_first
    )
    res = client.post(
        f"/api/v1/oncall/incidents/{incident.id}/ack",
        headers=_api_headers(oncall.eng),
    )
    assert res.status_code == 409
    assert res.get_json()["error"] == "Incident is already acknowledged"
    assert res.get_json()["incident"]["state"] == "acknowledged"


def test_api_refuses_outsiders(client, oncall):
    incident = _trigger(oncall)
    headers = _api_headers(_user("outsider"))
    assert (
        client.post(
            f"/api/v1/oncall/incidents/{incident.id}/ack", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/oncall/incidents/{incident.id}", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/oncall/incidents/{incident.id}/resolve", headers=headers
        ).status_code
        == 403
    )


def test_api_oncall_me(client, oncall, mocker):
    mocker.patch("app.blueprints.api_v1.utc_now", return_value=NOW)
    data = client.get("/api/v1/oncall/me", headers=_api_headers(oncall.eng)).get_json()
    assert data["on_call"]["schedule_name"] == oncall.schedule.name
    assert data["on_call"]["until"] == (SHIFT_START + WEEK).isoformat()
    # 8-week window, eng has every other week.
    assert len(data["upcoming_shifts"]) == 4
    assert data["can_be_paged"] is True

    outsider = _user("outsider")
    data = client.get("/api/v1/oncall/me", headers=_api_headers(outsider)).get_json()
    assert data["can_be_paged"] is False


def test_api_test_page_rings_only_me(client, oncall, mocker):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    res = client.post("/api/v1/oncall/test-page", headers=_api_headers(oncall.eng))
    assert res.status_code == 204
    frames = _frames(oncall.eng.id, "page")
    assert len(frames) == 1 and frames[0]["test"] is True
    assert send_page.call_args.args[0] == oncall.eng.id
    assert Incident.select().count() == 0


def test_page_cleared_push_names_who_acknowledged(oncall, mocker):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    incident = _trigger(oncall)
    oncall_service.acknowledge_incident(incident, oncall.lead)
    cleared.assert_called_once()
    assert cleared.call_args.args[0] == oncall.eng.id
    assert (
        cleared.call_args.kwargs["alert"]
        == f"✅ Acme site acknowledged by {oncall.lead.display_name}"
    )
    assert cleared.call_args.kwargs["data"] == {
        "type": "page_cleared",
        "incident_id": incident.id,
        "state": "acknowledged",
    }


def test_page_cleared_push_on_auto_resolve(oncall, mocker):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    oncall_service.resolve_incident(_trigger(oncall))
    assert cleared.call_args.kwargs["alert"] == "✅ Acme site resolved"


def test_page_push_goes_out_even_when_active_on_web(oncall, mocker):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    incident = _trigger(oncall)
    send_page.assert_called_once()
    assert send_page.call_args.args[0] == oncall.eng.id
    assert send_page.call_args.kwargs["data"]["incident_id"] == incident.id


# --- SSO group sync ---


def test_sso_login_syncs_groups(client, mocker):
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake"},
    )
    mocker.patch(
        "app.sso.oauth.authentik.parse_id_token",
        return_value={
            "sub": "sso-robert",
            "email": "robert@example.com",
            "given_name": "Robert",
            "groups": ["devops-lead", "Engineers"],
        },
    )
    with client.session_transaction() as sess:
        sess["nonce"] = "n"
    client.get("/auth")
    user = User.get(User.email == "robert@example.com")
    assert [u.id for u in users_in_group("devops-lead")] == [user.id]


def test_sso_login_without_groups_claim_keeps_groups(client, mocker):
    user = _user("emilio", ["director"])
    user.sso_id = "sso-emilio"
    user.save()
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake"},
    )
    mocker.patch(
        "app.sso.oauth.authentik.parse_id_token",
        return_value={"sub": "sso-emilio", "email": "emilio@example.com"},
    )
    mocker.patch("app.sso.oauth.authentik.userinfo", side_effect=RuntimeError("down"))
    with client.session_transaction() as sess:
        sess["nonce"] = "n"
    client.get("/auth")
    assert users_in_group("director") == [user]


# --- Admin UI ---


@pytest.fixture
def admin_client(client, oncall):
    return _login(client, _user("superadmin", admin=True))


def test_admin_pages_render(admin_client, oncall):
    incident = _trigger(oncall)
    for url in (
        "/admin/oncall",
        f"/admin/oncall/schedules/{oncall.schedule.id}",
        f"/admin/oncall/policies/{oncall.policy.id}",
        f"/admin/oncall/incidents/{incident.id}",
    ):
        res = admin_client.get(url)
        assert res.status_code == 200, url
    html = admin_client.get("/admin/oncall").get_data(as_text=True)
    assert "DevOps Primary" in html and "Group: director" in html


def test_admin_non_admin_is_refused(client, oncall):
    _login(client, oncall.eng)
    assert client.get("/admin/oncall").status_code == 302


# --- On-call admins (ONCALL_ADMIN_GROUP): the on-call pages, nothing else ---


@pytest.fixture
def oncall_admin(oncall):
    return _user("ops", ["oncall-admin"])


def test_oncall_admin_gets_the_oncall_pages(client, oncall, oncall_admin):
    incident = _trigger(oncall)
    _login(client, oncall_admin)
    for url in (
        "/admin/oncall",
        f"/admin/oncall/schedules/{oncall.schedule.id}",
        f"/admin/oncall/policies/{oncall.policy.id}",
        f"/admin/oncall/incidents/{incident.id}",
    ):
        assert client.get(url).status_code == 200, url

    newbie = _user("newbie")
    client.post(
        f"/admin/oncall/schedules/{oncall.schedule.id}/members",
        data={"user_id": newbie.id},
    )
    assert (
        OnCallScheduleMember.select()
        .where(OnCallScheduleMember.user == newbie)
        .exists()
    )

    # The admin sidebar offers them On-Call only.
    html = client.get("/admin/oncall").get_data(as_text=True)
    assert 'href="/admin/oncall"' in html
    assert "User Management" not in html and "Channel Management" not in html


def test_oncall_admin_gets_nothing_else_in_admin(client, oncall, oncall_admin):
    _login(client, oncall_admin)
    for url in ("/admin/", "/admin/users", "/admin/channels", "/admin/users/create"):
        assert client.get(url).status_code == 302, url
    # Can't grant groups (or admin) to anyone, themselves included.
    res = client.post(
        f"/admin/users/edit/{oncall.eng.id}",
        data={"local_groups": "oncall-admin", "role": "admin"},
    )
    assert res.status_code == 302
    assert not oncall_service.can_manage_oncall(oncall.eng)
    res = client.post(f"/admin/users/{oncall.eng.id}/deactivate")
    assert res.status_code == 302
    assert User.get_by_id(oncall.eng.id).is_active


def test_chat_sidebar_links_oncall_admins_to_oncall(client, oncall, oncall_admin):
    _login(client, oncall_admin)
    html = client.get("/chat").get_data(as_text=True)
    assert 'href="/admin/oncall"' in html and 'title="On-Call Admin"' in html
    assert 'title="Admin Settings"' not in html

    _login(client, oncall.eng)
    html = client.get("/chat").get_data(as_text=True)
    assert "admin-link" not in html


def test_oncall_admin_may_ack_any_incident(client, oncall, oncall_admin):
    incident = _trigger(oncall)
    assert oncall_service.user_in_chain(incident, oncall_admin)
    _login(client, oncall_admin)
    res = client.post(f"/oncall/incidents/{incident.id}/ack")
    assert res.get_json()["ok"] is True
    assert Incident.get_by_id(incident.id).state == "acknowledged"


def test_oncall_admin_group_can_come_from_sso(test_db, app):
    user = _user("sso-ops")
    assert not oncall_service.can_manage_oncall(user)
    sync_sso_groups(user, ["oncall-admin"])
    assert oncall_service.can_manage_oncall(user)
    sync_sso_groups(user, [])
    assert not oncall_service.can_manage_oncall(user)


def test_oncall_admin_group_name_is_configurable(test_db, app, monkeypatch):
    monkeypatch.setitem(app.config, "ONCALL_ADMIN_GROUP", "Pager-Admins")
    default_named = _user("old-name", ["oncall-admin"])
    renamed = _user("new-name", ["pager-admins"])
    assert oncall_service.can_manage_oncall(renamed)
    assert not oncall_service.can_manage_oncall(default_named)
    assert oncall_service.can_manage_oncall(_user("boss", admin=True))


def test_admin_override_and_member_management(admin_client, oncall):
    sid = oncall.schedule.id
    res = admin_client.post(
        f"/admin/oncall/schedules/{sid}/overrides",
        data={
            "user_id": "",
            "start": "2026-10-19T08:00",
            "end": "2026-10-26T08:00",
            "reason": "Vacation",
        },
    )
    assert res.status_code == 302
    override = OnCallOverride.get()
    assert override.user is None
    assert override.start_at == datetime.datetime(2026, 10, 19, 12, 0)  # 08:00 AST

    newbie = _user("newbie")
    admin_client.post(
        f"/admin/oncall/schedules/{sid}/members", data={"user_id": newbie.id}
    )
    members = list(
        OnCallScheduleMember.select()
        .where(OnCallScheduleMember.schedule == sid)
        .order_by(OnCallScheduleMember.position)
    )
    assert [m.user_id for m in members] == [oncall.eng.id, oncall.lead.id, newbie.id]

    admin_client.post(
        f"/admin/oncall/schedules/{sid}/members/{members[2].id}/move",
        data={"direction": "up"},
    )
    order = [
        m.user_id
        for m in OnCallScheduleMember.select()
        .where(OnCallScheduleMember.schedule == sid)
        .order_by(OnCallScheduleMember.position)
    ]
    assert order == [oncall.eng.id, newbie.id, oncall.lead.id]


def test_admin_settings_rejects_bad_timezone(admin_client, oncall):
    sid = oncall.schedule.id
    admin_client.post(
        f"/admin/oncall/schedules/{sid}/settings",
        data={
            "name": "X",
            "timezone": "Mars/Base",
            "handoff_weekday": "0",
            "handoff_time": "08:00",
        },
    )
    assert OnCallSchedule.get_by_id(sid).timezone == "America/Santo_Domingo"


def test_admin_level_editing(admin_client, oncall):
    pid = oncall.policy.id
    admin_client.post(
        f"/admin/oncall/policies/{pid}/levels",
        data={
            "level": "5",
            "delay_minutes": "15",
            "target_type": "user",
            "target_user_id": oncall.ceo.id,
        },
    )
    level = EscalationLevel.get(
        (EscalationLevel.policy == pid) & (EscalationLevel.level == 5)
    )
    assert level.target_user == oncall.ceo and level.delay_minutes == 15

    admin_client.post(
        f"/admin/oncall/policies/{pid}/levels/{level.id}",
        data={
            "level": "5",
            "delay_minutes": "5",
            "target_type": "group",
            "target_group": "Director",
        },
    )
    level = EscalationLevel.get_by_id(level.id)
    assert level.target_type == "group" and level.target_group == "director"
    assert level.target_user is None


def test_admin_test_page(admin_client, oncall):
    res = admin_client.post("/admin/oncall/test-page", data={"user_id": oncall.lead.id})
    assert res.status_code == 302
    assert _frames(oncall.lead.id, "page")[0]["test"] is True


def test_admin_edit_user_sets_local_groups(admin_client, oncall):
    user = oncall.director
    res = admin_client.post(
        f"/admin/users/edit/{user.id}",
        data={
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": "member",
            "local_groups": "director, CEO",
        },
    )
    assert res.status_code == 200
    names = {g.name for g in UserGroup.select().where(UserGroup.user == user)}
    assert names == {"director", "ceo"}
    page = admin_client.get(f"/admin/users/edit/{user.id}").get_data(as_text=True)
    assert "director, ceo" in page or "ceo, director" in page


# --- Website leads: D8 page requests through internal notify ---

NOTIFY_HEADERS = {"X-Internal-Key": "test-internal-notify-key"}


@pytest.fixture
def leads(oncall):
    """#website-leads with the "Website leads" policy (ka-ching: group sdr,
    then group ceo after 15 minutes), two SDRs, and the default outage setup
    from ``oncall`` (whose ceo is also the leads policy's level 2)."""
    workspace = Workspace.get(name="DevOcho")
    channel, policy = oncall_service.ensure_leads_paging(workspace)
    return SimpleNamespace(
        channel=channel,
        policy=policy,
        bot=User.get(User.username == oncall_service.D8_BOT_USERNAME),
        sam=_user("sam", ["sdr"]),
        ana=_user("ana", ["SDR"]),
        ceo=oncall.ceo,
        oncall=oncall,
    )


def _lead_page(client, key="d8-lead:101", channel="website-leads", **page):
    body = {
        "channel_name": channel,
        "message": "💰 **New website lead**: Dana Whitfield at Meridian Health",
        "page": {
            "key": key,
            "title": "New website lead: Dana Whitfield at Meridian Health",
            "details": "Filled in the Contact page form. Sam has it.",
            "url": "https://new-d8.devocho.com/marketing/leads/12",
            **page,
        },
    }
    return client.post("/api/v1/internal/notify", json=body, headers=NOTIFY_HEADERS)


def test_leads_policy_is_ka_ching_sdrs_then_ceo_after_15_minutes(leads):
    levels = oncall_service.sorted_levels(leads.policy)
    assert leads.policy.alarm == "ka-ching"
    assert leads.policy.no_oncall_start_level is None
    assert [
        (lv.level, lv.target_type, lv.target_group, lv.delay_minutes) for lv in levels
    ] == [
        (1, "group", "sdr", 0),
        (2, "group", "ceo", 15),
    ]
    assert leads.bot.display_name == "D8" and leads.bot.is_active is False
    # Idempotent, and an admin's edits survive a second run.
    leads.policy.alarm = "siren"
    leads.policy.save()
    _channel, again = oncall_service.ensure_leads_paging(Workspace.get(name="DevOcho"))
    assert again.id == leads.policy.id and again.alarm == "siren"


def test_a_lead_page_pages_every_sdr_with_the_ka_ching(client, leads):
    res = _lead_page(client)

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True and body["repeat"] is False
    incident = Incident.get_by_id(body["incident_id"])
    assert (incident.source, incident.dedupe_key) == ("d8", "d8:d8-lead:101")
    assert incident.title == "New website lead: Dana Whitfield at Meridian Health"
    assert incident.source_url == "https://new-d8.devocho.com/marketing/leads/12"
    assert incident.message.user == leads.bot
    assert _paged_ids(incident) == {leads.sam.id, leads.ana.id}
    assert incident.next_escalation_at == NOW + 15 * MINUTE
    (frame,) = _frames(leads.sam.id, "page")
    assert frame["incident"]["alarm"] == "ka-ching"
    assert frame["incident"]["id"] == incident.id
    # Nobody outside the policy is paged.
    assert _frames(leads.ceo.id, "page") == []
    assert _frames(leads.oncall.eng.id, "page") == []


def test_nobody_acknowledges_and_the_ceo_is_paged_at_15_minutes(client, leads):
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])

    assert oncall_service.run_escalations(NOW + 14 * MINUTE) == []
    oncall_service.run_escalations(NOW + 15 * MINUTE)

    assert _paged_ids(incident) == {leads.sam.id, leads.ana.id, leads.ceo.id}
    escalated = Message.select().order_by(Message.id.desc()).first()
    assert escalated.user == leads.bot
    assert "Escalated" in escalated.content and "Ceo" in escalated.content
    # Cumulative: the SDRs keep ringing alongside the CEO.
    resent = oncall_service.resend_pages(NOW + 16 * MINUTE)
    assert [i.id for i in resent] == [incident.id]
    assert len(_frames(leads.sam.id, "page")) == 2


def test_with_no_sdrs_the_ceo_is_paged_at_once(client, leads):
    UserGroup.delete().where(UserGroup.name == "sdr").execute()

    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])

    assert _paged_ids(incident) == {leads.ceo.id}


def test_an_sdr_acknowledges_and_d8_says_so(client, leads):
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])
    _login(client, leads.ana)

    res = client.post(f"/oncall/incidents/{incident.id}/ack")

    assert res.get_json()["ok"] is True
    incident = Incident.get_by_id(incident.id)
    assert (incident.state, incident.acknowledged_by) == ("acknowledged", leads.ana)
    assert incident.next_escalation_at is None
    assert _frames(leads.sam.id, "page_cleared")
    acked = Message.select().order_by(Message.id.desc()).first()
    assert acked.user == leads.bot and "Acknowledged" in acked.content
    assert oncall_service.run_escalations(NOW + 20 * MINUTE) == []


def test_the_same_key_while_open_posts_but_pages_nobody_again(client, leads):
    first = _lead_page(client).get_json()
    again = _lead_page(client).get_json()

    assert again == {"ok": True, "incident_id": first["incident_id"], "repeat": True}
    assert Incident.select().count() == 1
    assert len(_frames(leads.sam.id, "page")) == 1
    assert Message.select().where(Message.user == leads.bot).count() == 2  # both posted
    kinds = [e.kind for e in IncidentEvent.select().order_by(IncidentEvent.id)]
    assert "repeat_page" in kinds


def test_a_different_key_is_a_new_page(client, leads):
    _lead_page(client, key="d8-lead:101")
    _lead_page(client, key="d8-lead:104")

    assert Incident.select().count() == 2


@pytest.mark.parametrize("paging", ["no policy", "paging off"])
def test_without_active_paging_a_lead_is_only_posted(client, leads, paging):
    if paging == "no policy":
        leads.policy.delete_instance(recursive=True)
    else:
        leads.policy.is_active = False
        leads.policy.save()

    res = _lead_page(client)

    assert res.get_json() == {"ok": True, "incident_id": None, "repeat": False}
    assert Incident.select().count() == 0
    assert Message.select().where(Message.user == leads.bot).count() == 1


@pytest.mark.parametrize(
    "page",
    [
        "not an object",
        {"title": "no key"},
        {"key": "", "title": "empty key"},
        {"key": "k" * 201, "title": "key too long"},
        {"key": "d8-lead:1"},
        {"key": "d8-lead:1", "title": "   "},
        {"key": "d8-lead:1", "title": "t", "details": 5},
        {"key": "d8-lead:1", "title": "t", "url": "javascript:alert(1)"},
    ],
)
def test_a_malformed_page_request_is_refused(client, leads, page):
    res = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "website-leads", "message": "hi", "page": page},
        headers=NOTIFY_HEADERS,
    )

    assert res.status_code == 400
    assert Message.select().count() == 0


def test_a_page_request_needs_the_shared_secret(client, leads):
    res = client.post(
        "/api/v1/internal/notify",
        json={
            "channel_name": "website-leads",
            "message": "hi",
            "page": {"key": "d8-lead:1", "title": "t"},
        },
        headers={"X-Internal-Key": "wrong"},
    )

    assert res.status_code == 401
    assert Incident.select().count() == 0


def test_a_page_request_without_the_d8_bot_is_an_error(client, leads):
    leads.bot.delete_instance(recursive=True)

    res = _lead_page(client)

    assert res.status_code == 500
    assert "d8-bot" in res.get_json()["error"]


def test_a_lead_card_has_no_resolve_button(client, leads):
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])
    ChannelMember.create(user=leads.sam, channel=leads.channel)
    _login(client, leads.sam)

    html = client.get(f"/chat/channel/{leads.channel.id}").get_data(as_text=True)

    assert f"incident-card-{incident.id}" in html and "Acknowledge" in html
    assert f"/oncall/incidents/{incident.id}/resolve" not in html


def test_an_outage_card_keeps_its_resolve_button(client, oncall):
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    incident_id = res.get_json()["incident_id"]
    ChannelMember.create(user=oncall.eng, channel=oncall.channel)
    _login(client, oncall.eng)

    html = client.get(f"/chat/channel/{oncall.channel.id}").get_data(as_text=True)

    assert f"/oncall/incidents/{incident_id}/resolve" in html


def test_a_lead_push_is_worded_as_a_lead(client, leads, mocker):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])
    send_page.reset_mock()

    oncall_service._push_pages(incident.id, [leads.sam.id])

    kwargs = send_page.call_args.kwargs
    assert kwargs["title"] == "💰 New website lead: Dana Whitfield at Meridian Health"
    assert kwargs["body"] == "Filled in the Contact page form. Sam has it."
    assert kwargs["data"]["alarm"] == "ka-ching"


def test_an_outage_push_is_still_worded_as_an_outage(oncall, mocker):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    _trigger(oncall)

    kwargs = send_page.call_args.kwargs
    assert kwargs["title"] == "🚨 DOWN: Acme site"
    assert kwargs["data"]["alarm"] == "siren"


def test_an_incident_whose_policy_is_gone_rings_the_siren(client, leads):
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])
    leads.policy.delete_instance(recursive=True)

    assert (
        oncall_service.serialize_incident(Incident.get_by_id(incident.id))["alarm"]
        == "siren"
    )


def test_the_test_page_rings_the_alarms_i_can_be_paged_with(client, leads):
    both = _user("both", ["sdr", "director"])
    outsider = _user("outsider")
    cases = {
        leads.sam: ["ka-ching"],
        leads.oncall.eng: ["siren"],
        both: ["siren", "ka-ching"],
        outsider: ["siren"],
    }
    for user, alarms in cases.items():
        _login(client, user)
        assert client.post("/oncall/test-page").status_code == 204
        frames = _frames(user.id, "page")
        assert [f["incident"]["alarm"] for f in frames] == alarms, user.username
        assert all(f["test"] for f in frames)
        assert len({f["incident"]["id"] for f in frames}) == len(frames)
    assert Incident.select().count() == 0


def test_an_admin_sets_a_policys_alarm(admin_client, leads):
    url = f"/admin/oncall/policies/{leads.oncall.policy.id}/settings"
    page = admin_client.get(f"/admin/oncall/policies/{leads.policy.id}")
    assert 'value="ka-ching" selected' in page.get_data(as_text=True)

    admin_client.post(
        url, data={"name": "DevOps Uptime", "is_active": "on", "alarm": "ka-ching"}
    )
    assert (
        oncall_service.EscalationPolicy.get_by_id(leads.oncall.policy.id).alarm
        == "ka-ching"
    )

    admin_client.post(
        url, data={"name": "DevOps Uptime", "is_active": "on", "alarm": "kazoo"}
    )
    assert (
        oncall_service.EscalationPolicy.get_by_id(leads.oncall.policy.id).alarm
        == "ka-ching"
    )

    # A form without the field (an older page) keeps the alarm.
    admin_client.post(url, data={"name": "DevOps Uptime", "is_active": "on"})
    assert (
        oncall_service.EscalationPolicy.get_by_id(leads.oncall.policy.id).alarm
        == "ka-ching"
    )


def test_the_helpdesk_post_is_unchanged_without_a_page(client, leads):
    from tests.test_api_v1 import _seed_helpdesk

    helpdesk_bot, channel = _seed_helpdesk()

    res = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": channel.name, "message": "[NEW] Ticket #1"},
        headers=NOTIFY_HEADERS,
    )

    assert res.get_json() == {"ok": True}
    assert Message.get(Message.content == "[NEW] Ticket #1").user == helpdesk_bot
    assert Incident.select().count() == 0


# --- The interview day: the website's page requests naming who has to act ---


@pytest.fixture
def interview(leads):
    """#interview-day with the "Interview day" policy (chime: whoever a page
    names, else group hr, then group engineering-manager after 10 minutes),
    two graders, an HR coordinator and an engineering manager."""
    workspace = Workspace.get(name="DevOcho")
    channel, policy = oncall_service.ensure_interview_paging(workspace)
    return SimpleNamespace(
        channel=channel,
        policy=policy,
        bot=leads.bot,
        g1=_user("grader1"),
        g2=_user("grader2"),
        hr=_user("hrc", ["hr"]),
        em=_user("emgr", ["engineering-manager"]),
        leads=leads,
    )


def _interview_page(client, key="iv-grade:20:grader1@example.com", **page):
    body = {
        "channel_name": "interview-day",
        "message": "🔔 **Please grade** Jose Pasante's Developer internship exercise.",
        "page": {
            "key": key,
            "title": "Please grade Jose Pasante's exercise",
            "details": "He finished the Developer internship exercise at 10:47.",
            "url": "https://interview.local/admin/sessions/20/grade",
            "to": ["grader1@example.com"],
            **page,
        },
    }
    return client.post("/api/v1/internal/notify", json=body, headers=NOTIFY_HEADERS)


def test_interview_policy_is_a_private_chime_then_the_engineering_managers(interview):
    levels = oncall_service.sorted_levels(interview.policy)
    assert interview.policy.alarm == "chime"
    assert interview.channel.is_private is True
    assert [
        (lv.level, lv.target_type, lv.target_group, lv.delay_minutes) for lv in levels
    ] == [
        (1, "group", "hr", 0),
        (2, "group", "engineering-manager", 10),
    ]
    assert ChannelMember.get_or_none(user=interview.bot, channel=interview.channel)
    # Idempotent, and an admin's edits survive a second run.
    interview.policy.alarm = "ka-ching"
    interview.policy.save()
    _channel, again = oncall_service.ensure_interview_paging(
        Workspace.get(name="DevOcho")
    )
    assert again.id == interview.policy.id and again.alarm == "ka-ching"


def test_a_page_naming_people_pages_exactly_them_with_the_chime(client, interview):
    res = _interview_page(
        client, to=["grader1@example.com", "GRADER2@Example.com", "nobody@example.com"]
    )

    body = res.get_json()
    assert res.status_code == 200
    assert body["repeat"] is False and body["unmatched"] == ["nobody@example.com"]
    incident = Incident.get_by_id(body["incident_id"])
    assert _paged_ids(incident) == {interview.g1.id, interview.g2.id}
    assert incident.current_level == 1
    assert incident.next_escalation_at == NOW + 10 * MINUTE
    (frame,) = _frames(interview.g1.id, "page")
    assert frame["incident"]["alarm"] == "chime"
    # HR (the first level) and the engineering manager are not paged yet.
    assert _frames(interview.hr.id, "page") == []
    assert _frames(interview.em.id, "page") == []


def test_nobody_named_acknowledges_and_the_engineering_manager_is_paged(
    client, interview
):
    incident = Incident.get_by_id(_interview_page(client).get_json()["incident_id"])

    assert oncall_service.run_escalations(NOW + 9 * MINUTE) == []
    oncall_service.run_escalations(NOW + 10 * MINUTE)

    assert _paged_ids(incident) == {interview.g1.id, interview.em.id}
    assert _frames(interview.hr.id, "page") == []


def test_a_named_person_acknowledges_their_own_page(client, interview):
    first = _interview_page(client).get_json()
    second = _interview_page(
        client, key="iv-grade:20:grader2@example.com", to=["grader2@example.com"]
    ).get_json()
    _login(client, interview.g1)

    res = client.post(f"/oncall/incidents/{first['incident_id']}/ack")

    assert res.get_json()["ok"] is True
    assert Incident.get_by_id(first["incident_id"]).state == "acknowledged"
    # Each grader has a page of their own: the other is still ringing.
    assert Incident.get_by_id(second["incident_id"]).state == "triggered"


def test_a_page_naming_nobody_known_pages_hr(client, interview):
    res = _interview_page(client, to=["stranger@example.com"])

    body = res.get_json()
    assert body["unmatched"] == ["stranger@example.com"]
    assert _paged_ids(Incident.get_by_id(body["incident_id"])) == {interview.hr.id}


def test_an_inactive_person_is_not_paged(client, interview):
    interview.g1.is_active = False
    interview.g1.save()

    body = _interview_page(client).get_json()

    assert body["unmatched"] == ["grader1@example.com"]
    assert _paged_ids(Incident.get_by_id(body["incident_id"])) == {interview.hr.id}


def test_a_repeat_of_an_open_page_still_reports_who_matched_nobody(client, interview):
    first = _interview_page(client, to=["grader1@example.com", "x@example.com"])
    again = _interview_page(client, to=["grader1@example.com", "x@example.com"])

    assert again.get_json() == {
        "ok": True,
        "incident_id": first.get_json()["incident_id"],
        "repeat": True,
        "unmatched": ["x@example.com"],
    }
    assert len(_frames(interview.g1.id, "page")) == 1


@pytest.mark.parametrize(
    "to",
    [
        "grader1@example.com",
        [],
        ["not an email"],
        [5],
        ["a@example.com"] * 11,
        ["a" * 250 + "@example.com"],
    ],
)
def test_a_malformed_to_is_refused(client, interview, to):
    res = _interview_page(client, to=to)

    assert res.status_code == 400
    assert Incident.select().count() == 0


def test_a_chime_sends_one_ordinary_notification_not_the_phone_alarm(
    client, interview, mocker
):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    send_to_user = mocker.patch.object(oncall_service.push_service, "send_to_user")
    incident = Incident.get_by_id(_interview_page(client).get_json()["incident_id"])
    send_to_user.reset_mock()

    oncall_service._push_pages(incident.id, [interview.g1.id])

    send_page.assert_not_called()
    kwargs = send_to_user.call_args.kwargs
    assert kwargs["title"] == "🔔 Please grade Jose Pasante's exercise"
    assert kwargs["body"] == "He finished the Developer internship exercise at 10:47."
    assert kwargs["data"]["type"] == "page_notice"
    assert kwargs["data"]["conversation_id_str"] == f"channel_{interview.channel.id}"

    # The minute's re-send rings the browser again, not the phone.
    send_to_user.reset_mock()
    oncall_service._push_pages(incident.id, [interview.g1.id], repeat=True)
    send_to_user.assert_not_called()


def test_the_test_page_chimes_for_someone_in_the_interview_channel(
    client, interview, mocker
):
    send_page = mocker.patch.object(oncall_service.push_service, "send_page")
    send_to_user = mocker.patch.object(oncall_service.push_service, "send_to_user")
    ChannelMember.create(user=interview.g1, channel=interview.channel)
    _login(client, interview.g1)

    assert client.post("/oncall/test-page").status_code == 204

    frames = _frames(interview.g1.id, "page")
    assert [f["incident"]["alarm"] for f in frames] == ["chime"]
    send_page.assert_not_called()
    assert send_to_user.call_args.kwargs["title"] == "🔔 Test page"


def test_an_admin_can_choose_the_chime(admin_client, interview):
    page = admin_client.get(f"/admin/oncall/policies/{interview.policy.id}")
    assert 'value="chime" selected' in page.get_data(as_text=True)


def test_acknowledging_a_chime_sends_the_phone_nothing_more(client, interview, mocker):
    send_page_cleared = mocker.patch.object(
        oncall_service.push_service, "send_page_cleared"
    )
    incident = Incident.get_by_id(_interview_page(client).get_json()["incident_id"])
    _login(client, interview.g1)

    client.post(f"/oncall/incidents/{incident.id}/ack")

    assert _frames(interview.g1.id, "page_cleared")
    send_page_cleared.assert_not_called()


# --- Directed pages: D8 runs its own "accept the lead" rotation ---
#
# D8 pages one SDR with page.escalate false, gives them 5 minutes to accept the
# lead in D8, then resolves that page by key and pages the next SDR, and
# finally the CEO. D8-Chat only rings whoever each request names.

RESOLVE_URL = "/api/v1/internal/page/resolve"


def _directed_page(client, to=("sam@example.com",), key="d8-lead:101", **page):
    return _lead_page(client, key=key, to=list(to), escalate=False, **page)


def _resolve_page(client, key="d8-lead:101", reason="accepted", **body):
    return client.post(
        RESOLVE_URL,
        json={"key": key, "reason": reason, **body},
        headers=NOTIFY_HEADERS,
    )


def test_a_directed_page_rings_only_who_it_names_and_never_escalates(client, leads):
    body = _directed_page(client).get_json()

    incident = Incident.get_by_id(body["incident_id"])
    assert body["repeat"] is False and body["unmatched"] == []
    assert _paged_ids(incident) == {leads.sam.id}
    assert incident.next_escalation_at is None
    (frame,) = _frames(leads.sam.id, "page")
    assert frame["incident"]["alarm"] == "ka-ching"
    assert frame["incident"]["next_escalation_at"] is None
    # Long past the policy's 15 minutes: no level fires, the CEO never rings.
    for minutes in (15, 16, 60, 24 * 60):
        assert oncall_service.run_escalations(NOW + minutes * MINUTE) == []
    assert _paged_ids(Incident.get_by_id(incident.id)) == {leads.sam.id}
    assert _frames(leads.ana.id, "page") == []
    assert _frames(leads.ceo.id, "page") == []
    # It keeps ringing Sam until it stops, like any page.
    assert oncall_service.resend_pages(NOW + 2 * MINUTE) == [incident]
    assert len(_frames(leads.sam.id, "page")) == 2
    triggered = IncidentEvent.get(
        (IncidentEvent.incident == incident) & (IncidentEvent.kind == "triggered")
    )
    assert json.loads(triggered.details)["escalate"] is False


def test_a_directed_page_never_falls_back_to_the_admins(client, leads):
    admin = _user("boss", admin=True)
    EscalationLevel.delete().where(EscalationLevel.policy == leads.policy).execute()

    incident = Incident.get_by_id(_directed_page(client).get_json()["incident_id"])

    assert _paged_ids(incident) == {leads.sam.id}
    assert oncall_service.run_escalations(NOW + DAY) == []
    assert _frames(admin.id, "page") == []
    kinds = {e.kind for e in IncidentEvent.select()}
    assert "fallback_admins" not in kinds and "escalated" not in kinds


def test_a_directed_page_without_a_policy_still_rings_who_it_names(client, leads):
    leads.policy.delete_instance(recursive=True)

    incident = Incident.get_by_id(_directed_page(client).get_json()["incident_id"])

    assert incident.policy is None
    assert _paged_ids(incident) == {leads.sam.id}
    assert oncall_service.run_escalations(NOW + DAY) == []
    # No policy to choose the alarm: the default.
    assert _frames(leads.sam.id, "page")[0]["incident"]["alarm"] == "siren"


def test_a_directed_page_with_paging_switched_off_is_only_posted(client, leads):
    leads.policy.is_active = False
    leads.policy.save()

    res = _directed_page(client)

    assert res.get_json() == {
        "ok": True,
        "incident_id": None,
        "repeat": False,
        "unmatched": [],
    }
    assert Incident.select().count() == 0
    assert Message.select().where(Message.user == leads.bot).count() == 1


def test_a_directed_page_naming_nobody_known_pages_nobody(client, leads):
    admin = _user("boss", admin=True)

    res = _directed_page(client, to=["stranger@example.com"])

    assert res.get_json() == {
        "ok": True,
        "incident_id": None,
        "repeat": False,
        "unmatched": ["stranger@example.com"],
    }
    assert Incident.select().count() == 0
    assert Message.select().where(Message.user == leads.bot).count() == 1
    for user in (leads.sam, leads.ana, leads.ceo, admin):
        assert _frames(user.id, "page") == []


def test_a_directed_repeat_while_open_pages_nobody_again(client, leads):
    first = _directed_page(client).get_json()
    again = _directed_page(client, to=["ana@example.com"]).get_json()

    assert again == {
        "ok": True,
        "incident_id": first["incident_id"],
        "repeat": True,
        "unmatched": [],
    }
    assert Incident.select().count() == 1
    assert _paged_ids(Incident.get_by_id(first["incident_id"])) == {leads.sam.id}
    assert _frames(leads.ana.id, "page") == []


@pytest.mark.parametrize(
    "page",
    [
        {"escalate": False},
        {"escalate": "false"},
        {"escalate": 0},
        {"escalate": False, "to": []},
    ],
)
def test_a_malformed_escalate_is_refused(client, leads, page):
    res = client.post(
        "/api/v1/internal/notify",
        json={
            "channel_name": "website-leads",
            "message": "hi",
            "page": {"key": "d8-lead:1", "title": "t", **page},
        },
        headers=NOTIFY_HEADERS,
    )

    assert res.status_code == 400
    assert "escalate" in res.get_json()["error"]
    assert Message.select().count() == 0


def test_escalate_true_or_null_is_todays_page(client, leads):
    explicit = _lead_page(client, key="d8-lead:1", escalate=True).get_json()
    null = _lead_page(client, key="d8-lead:2", escalate=None).get_json()

    for body in (explicit, null):
        incident = Incident.get_by_id(body["incident_id"])
        assert _paged_ids(incident) == {leads.sam.id, leads.ana.id}
        assert incident.next_escalation_at == NOW + 15 * MINUTE


def test_d8_resolves_its_page_and_every_pager_stops(client, leads, mocker):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    incident = Incident.get_by_id(_directed_page(client).get_json()["incident_id"])
    posts = Message.select().count()

    res = _resolve_page(client, by="Sam@Example.com")

    assert (res.status_code, res.get_json()) == (200, {"resolved": True})
    incident = Incident.get_by_id(incident.id)
    assert (incident.state, incident.resolved_by) == ("resolved", leads.sam)
    assert incident.resolved_at == NOW
    # Stopped on every socket and on the phone, with D8's reason.
    assert _frames(leads.sam.id, "page_cleared")[-1]["state"] == "resolved"
    assert cleared.call_args.args[0] == leads.sam.id
    assert cleared.call_args.kwargs["alert"] == (
        "✅ New website lead: Dana Whitfield at Meridian Health: Accepted in D8"
    )
    assert oncall_service.active_pages_for(leads.sam) == []
    assert oncall_service.resend_pages(NOW + 5 * MINUTE) == []
    # Who and why on the timeline; nothing posted in the channel.
    resolved = IncidentEvent.get(
        (IncidentEvent.incident == incident) & (IncidentEvent.kind == "resolved")
    )
    assert resolved.user == leads.sam
    assert json.loads(resolved.details) == {
        "detail": "Accepted in D8",
        "reason": "accepted",
        "by": "Sam@Example.com",
    }
    assert Message.select().count() == posts


def test_resolving_twice_or_an_unknown_key_is_harmless(client, leads):
    _directed_page(client)

    assert _resolve_page(client, reason="expired").get_json() == {"resolved": True}
    assert _resolve_page(client, reason="expired").get_json() == {"resolved": False}
    assert _resolve_page(client, key="d8-lead:999").get_json() == {"resolved": False}
    assert IncidentEvent.select().where(IncidentEvent.kind == "resolved").count() == 1


def test_resolve_by_someone_unknown_still_resolves(client, leads):
    incident_id = _directed_page(client).get_json()["incident_id"]

    res = _resolve_page(client, reason="cancelled", by="gone@example.com")

    assert res.get_json() == {"resolved": True}
    incident = Incident.get_by_id(incident_id)
    assert incident.state == "resolved" and incident.resolved_by is None
    resolved = IncidentEvent.get(IncidentEvent.kind == "resolved")
    assert resolved.user is None
    assert json.loads(resolved.details)["by"] == "gone@example.com"


def test_the_rotation_pages_the_next_sdr_after_an_expired_page(client, leads):
    """D8's whole flow on one key: Sam, then Ana, then the CEO."""
    ChannelMember.create(user=leads.sam, channel=leads.channel)
    first = _directed_page(client).get_json()["incident_id"]

    assert _resolve_page(client, reason="expired").get_json() == {"resolved": True}
    second = _directed_page(client, to=["ana@example.com"]).get_json()
    assert second["repeat"] is False and second["incident_id"] != first
    assert _paged_ids(Incident.get_by_id(second["incident_id"])) == {leads.ana.id}

    assert _resolve_page(client, reason="rejected", by="ana@example.com").get_json()
    third = _directed_page(client, to=["ceo@example.com"]).get_json()
    assert _paged_ids(Incident.get_by_id(third["incident_id"])) == {leads.ceo.id}
    assert _resolve_page(client, reason="accepted", by="ceo@example.com").get_json()

    assert oncall_service.run_escalations(NOW + DAY) == []
    assert [len(_frames(u.id, "page")) for u in (leads.sam, leads.ana, leads.ceo)] == [
        1,
        1,
        1,
    ]
    # Each card says how D8 ended that page.
    _login(client, leads.sam)
    html = client.get(f"/chat/channel/{leads.channel.id}").get_data(as_text=True)
    assert "Expired in D8" in html
    assert "Rejected in D8" in html and "<strong>Ana</strong>" in html
    assert "Accepted in D8" in html and "<strong>Ceo</strong>" in html
    assert "monitor back up" not in html


def test_the_admin_timeline_says_who_paged_and_why_d8_stopped_it(admin_client, leads):
    incident_id = _directed_page(admin_client).get_json()["incident_id"]
    _resolve_page(admin_client, reason="rejected", by="sam@example.com")

    html = admin_client.get(f"/admin/oncall/incidents/{incident_id}").get_data(
        as_text=True
    )

    assert oncall_service.NO_ESCALATION in html
    assert "Rejected in D8" in html


def test_resolve_also_stops_an_escalating_lead(client, leads):
    incident = Incident.get_by_id(_lead_page(client).get_json()["incident_id"])

    assert _resolve_page(client, reason="cancelled").get_json() == {"resolved": True}

    assert Incident.get_by_id(incident.id).state == "resolved"
    assert _frames(leads.sam.id, "page_cleared") and _frames(
        leads.ana.id, "page_cleared"
    )
    assert oncall_service.run_escalations(NOW + 15 * MINUTE) == []
    assert _frames(leads.ceo.id, "page") == []


def test_resolve_after_an_acknowledge_closes_it_quietly(client, leads, mocker):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    incident_id = _directed_page(client).get_json()["incident_id"]
    _login(client, leads.sam)
    client.post(f"/oncall/incidents/{incident_id}/ack")
    cleared.reset_mock()

    assert _resolve_page(client, by="sam@example.com").get_json() == {"resolved": True}

    assert Incident.get_by_id(incident_id).state == "resolved"
    # The pager already stopped at the acknowledge: nothing more to clear.
    cleared.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        ["d8-lead:101", "accepted"],
        {"reason": "accepted"},
        {"key": "", "reason": "accepted"},
        {"key": "k" * 201, "reason": "accepted"},
        {"key": 101, "reason": "accepted"},
        {"key": "d8-lead:101"},
        {"key": "d8-lead:101", "reason": "done"},
        {"key": "d8-lead:101", "reason": ["accepted"]},
        {"key": "d8-lead:101", "reason": "accepted", "by": "not an email"},
        {"key": "d8-lead:101", "reason": "accepted", "by": 5},
    ],
)
def test_a_malformed_resolve_is_refused(client, leads, body):
    incident_id = _directed_page(client).get_json()["incident_id"]

    res = client.post(RESOLVE_URL, json=body, headers=NOTIFY_HEADERS)

    assert res.status_code == 400
    assert "reason" in res.get_json()["error"]
    assert Incident.get_by_id(incident_id).state == "triggered"


def test_resolve_needs_the_shared_secret(client, leads):
    incident_id = _directed_page(client).get_json()["incident_id"]

    for headers in ({}, {"X-Internal-Key": "wrong"}):
        res = client.post(
            RESOLVE_URL,
            json={"key": "d8-lead:101", "reason": "accepted"},
            headers=headers,
        )
        assert res.status_code == 401

    assert Incident.get_by_id(incident_id).state == "triggered"


# --- Accept and Reject: answering D8's lead offers from D8-Chat ---

LEAD_RESPONSE_URL = "http://new-d8/api/d8-chat/lead-offers/respond"
OFFER_KEY = "lead-12-offer-31"
LEAD_URL = "https://new-d8.devocho.com/marketing/leads/12"


def _d8_answer(status, body=None):
    """What ``requests.post`` returns for D8's answer: ``body`` as its JSON,
    or no JSON at all (an HTML error page) when None."""

    def _json():
        if body is None:
            raise ValueError("not JSON")
        return body

    return SimpleNamespace(status_code=status, json=_json)


@pytest.fixture
def offer(client, leads, app, mocker):
    """D8-Chat knows D8's answer endpoint, and D8 has offered a lead to Sam:
    a directed page with the ka-ching. ``offer.d8`` is the mocked call to D8
    (it answers "accepted" unless a test says otherwise)."""
    app.config["D8_LEAD_RESPONSE_URL"] = LEAD_RESPONSE_URL
    incident_id = _directed_page(client, key=OFFER_KEY).get_json()["incident_id"]
    ChannelMember.create(user=leads.sam, channel=leads.channel)
    d8 = mocker.patch(
        "app.services.lead_response.requests.post",
        return_value=_d8_answer(
            200, {"ok": True, "status": "accepted", "lead_url": LEAD_URL}
        ),
    )
    return SimpleNamespace(incident=Incident.get_by_id(incident_id), d8=d8, leads=leads)


def _answer(client, incident, action="accept", user=None, htmx=False):
    if user is not None:
        _login(client, user)
    headers = {"HX-Request": "true"} if htmx else {}
    return client.post(f"/oncall/incidents/{incident.id}/{action}", headers=headers)


def _card(client, channel):
    return client.get(f"/chat/channel/{channel.id}").get_data(as_text=True)


def test_a_lead_card_offers_accept_and_reject_in_green(client, offer):
    incident = offer.incident
    _login(client, offer.leads.sam)

    html = _card(client, offer.leads.channel)

    assert f"/oncall/incidents/{incident.id}/accept" in html
    assert f"/oncall/incidents/{incident.id}/reject" in html
    assert 'hx-confirm="Reject this lead? It goes to the next person."' in html
    assert f"/oncall/incidents/{incident.id}/ack" not in html
    card = html[html.index(f'id="incident-card-{incident.id}"') :]
    assert "incident-lead" in card[:200]
    assert '<span class="badge bg-success"><i class="bi bi-broadcast' in card
    # The pager is told the same.
    assert oncall_service.serialize_incident(incident)["lead_response"] is True
    (frame,) = _frames(offer.leads.sam.id, "page")
    assert frame["incident"]["lead_response"] is True


def test_without_d8s_answer_endpoint_a_lead_keeps_acknowledge(client, offer, app):
    app.config["D8_LEAD_RESPONSE_URL"] = ""
    _login(client, offer.leads.sam)

    html = _card(client, offer.leads.channel)

    assert f"/oncall/incidents/{offer.incident.id}/ack" in html
    assert f"/oncall/incidents/{offer.incident.id}/accept" not in html
    assert '<span class="badge bg-success"><i class="bi bi-broadcast' in html
    assert oncall_service.serialize_incident(offer.incident)["lead_response"] is False


def test_an_outage_card_stays_red_with_acknowledge(client, offer, app):
    oncall = offer.leads.oncall
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    incident = Incident.get_by_id(res.get_json()["incident_id"])
    ChannelMember.create(user=oncall.eng, channel=oncall.channel)
    _login(client, oncall.eng)

    html = _card(client, oncall.channel)

    assert f"/oncall/incidents/{incident.id}/ack" in html
    assert f"/oncall/incidents/{incident.id}/accept" not in html
    assert '<span class="badge bg-danger"><i class="bi bi-broadcast' in html
    assert "incident-lead" not in html
    assert oncall_service.serialize_incident(incident)["lead_response"] is False


def test_an_interview_day_page_is_not_a_lead(client, interview, app, mocker):
    app.config["D8_LEAD_RESPONSE_URL"] = LEAD_RESPONSE_URL
    d8 = mocker.patch("app.services.lead_response.requests.post")
    incident = Incident.get_by_id(_interview_page(client).get_json()["incident_id"])
    ChannelMember.create(user=interview.g1, channel=interview.channel)
    _login(client, interview.g1)

    html = _card(client, interview.channel)

    assert incident.source == "d8"
    assert f"/oncall/incidents/{incident.id}/ack" in html
    assert f"/oncall/incidents/{incident.id}/accept" not in html
    assert '<span class="badge bg-danger"><i class="bi bi-broadcast' in html
    assert oncall_service.serialize_incident(incident)["lead_response"] is False
    res = _answer(client, incident)
    assert res.status_code == 400
    d8.assert_not_called()


def test_accept_sends_the_answer_to_d8_and_stops_the_page(client, offer, mocker):
    cleared = mocker.patch.object(oncall_service.push_service, "send_page_cleared")
    sam = offer.leads.sam
    posts = Message.select().count()

    res = _answer(client, offer.incident, user=sam)

    assert res.status_code == 200
    assert res.get_json() == {
        "ok": True,
        "stopped": True,
        "status": "accepted",
        "lead_url": LEAD_URL,
        "message": "The lead is yours.",
        "code": None,
        "incident_id": offer.incident.id,
    }
    offer.d8.assert_called_once_with(
        LEAD_RESPONSE_URL,
        json={"key": OFFER_KEY, "email": "sam@example.com", "action": "accept"},
        headers={"X-Internal-Key": "test-internal-notify-key"},
        timeout=5,
    )
    incident = Incident.get_by_id(offer.incident.id)
    assert (incident.state, incident.resolved_by) == ("resolved", sam)
    assert _frames(sam.id, "page_cleared")[-1]["state"] == "resolved"
    assert cleared.call_args.kwargs["alert"].endswith(": Accepted in D8")
    assert oncall_service.active_pages_for(sam) == []
    resolved = IncidentEvent.get(
        (IncidentEvent.incident == incident) & (IncidentEvent.kind == "resolved")
    )
    assert json.loads(resolved.details) == {
        "detail": "Accepted in D8",
        "reason": "accepted",
        "by": "sam@example.com",
    }
    assert "Accepted in D8" in _card(client, offer.leads.channel)
    # Nothing posted: D8 posts its own step to the channel.
    assert Message.select().count() == posts
    # D8 resolves the page too; it finds nothing left open.
    assert _resolve_page(client, key=OFFER_KEY).get_json() == {"resolved": False}


def test_d8_resolving_first_is_harmless(client, offer):
    """D8 resolves the page inside its answer: ours finds it closed."""

    def d8_answers(*args, **kwargs):
        oncall_service.resolve_page(OFFER_KEY, "accepted", by="sam@example.com")
        return _d8_answer(200, {"ok": True, "status": "accepted", "lead_url": LEAD_URL})

    offer.d8.side_effect = d8_answers

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.get_json()["ok"] is True
    assert Incident.get_by_id(offer.incident.id).state == "resolved"
    assert IncidentEvent.select().where(IncidentEvent.kind == "resolved").count() == 1


def test_reject_sends_the_answer_and_the_lead_moves_on(client, offer):
    offer.d8.return_value = _d8_answer(200, {"ok": True, "status": "rejected"})

    res = _answer(client, offer.incident, action="reject", user=offer.leads.sam)

    body = res.get_json()
    assert (body["ok"], body["status"], body["lead_url"]) == (True, "rejected", None)
    assert body["message"] == "It goes to the next person."
    assert offer.d8.call_args.kwargs["json"]["action"] == "reject"
    assert Incident.get_by_id(offer.incident.id).state == "resolved"
    assert "Rejected in D8" in _card(client, offer.leads.channel)


def test_a_lead_url_that_is_not_http_is_dropped(client, offer):
    offer.d8.return_value = _d8_answer(
        200, {"ok": True, "status": "accepted", "lead_url": "javascript:alert(1)"}
    )

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.get_json()["lead_url"] is None


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (
            403,
            {"error": "not_yours"},
            "D8 says this lead was offered to someone else.",
        ),
        (
            404,
            {"error": "unknown_offer"},
            "D8 has no offer for this page, so there is nothing to accept or reject.",
        ),
        (409, {"error": "offer_closed"}, "This offer has already closed in D8."),
        (
            409,
            {"error": "offer_closed", "message": "Ana accepted it first."},
            "Ana accepted it first.",
        ),
    ],
)
def test_d8_refuses_and_the_page_stops_quietly(client, offer, status, body, message):
    offer.d8.return_value = _d8_answer(status, body)
    sam = offer.leads.sam
    posts = Message.select().count()

    res = _answer(client, offer.incident, user=sam)

    assert res.status_code == 200
    assert res.get_json() == {
        "ok": False,
        "stopped": True,
        "status": None,
        "lead_url": None,
        "message": message,
        "code": body["error"],
        "incident_id": offer.incident.id,
    }
    # Nothing left for Sam to answer: it stops ringing instead of ringing on.
    incident = Incident.get_by_id(offer.incident.id)
    assert (incident.state, incident.acknowledged_by) == ("acknowledged", sam)
    assert _frames(sam.id, "page_cleared")
    assert oncall_service.active_pages_for(sam) == []
    assert Message.select().count() == posts
    # D8's own resolve still closes it.
    assert _resolve_page(client, key=OFFER_KEY, reason="expired").get_json() == {
        "resolved": True
    }


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (_d8_answer(401, {"error": "unauthorized"}), "D8-Chat's key"),
        (_d8_answer(500, {"error": "boom"}), "HTTP 500"),
        (_d8_answer(503), "HTTP 503"),
        # A 404 without D8's code is a wrong URL, not "no such offer".
        (_d8_answer(404), "HTTP 404"),
        (_d8_answer(403, {"error": "forbidden"}), "HTTP 403"),
        (_d8_answer(200, {"ok": True, "status": "maybe"}), "HTTP 200"),
        (_d8_answer(200, {"status": "accepted"}), "HTTP 200"),
        (_d8_answer(200, ["ok"]), "HTTP 200"),
    ],
)
def test_an_answer_d8_never_took_keeps_the_page_ringing(
    client, offer, answer, fragment
):
    offer.d8.return_value = answer

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.status_code == 502
    assert fragment in res.get_json()["error"]
    assert Incident.get_by_id(offer.incident.id).state == "triggered"
    assert oncall_service.active_pages_for(offer.leads.sam) == [offer.incident]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            lead_response.requests.ConnectionError("refused"),
            "Couldn't reach D8. Try again.",
        ),
        (
            lead_response.requests.Timeout("read timed out"),
            "D8 didn't answer in time. Try again.",
        ),
    ],
)
def test_d8_out_of_reach_keeps_the_page_ringing(client, offer, error, message):
    offer.d8.side_effect = error

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert (res.status_code, res.get_json()) == (502, {"error": message})
    assert Incident.get_by_id(offer.incident.id).state == "triggered"
    assert oncall_service.active_pages_for(offer.leads.sam) == [offer.incident]


def test_only_the_person_the_lead_was_offered_to_may_answer(client, offer):
    admin = _user("boss", admin=True)
    # Ana is an SDR and the admin may acknowledge anything, but neither was paged.
    for user in (offer.leads.ana, admin, offer.leads.ceo):
        res = _answer(client, offer.incident, user=user)
        assert res.status_code == 403
        assert "offered to" in res.get_json()["error"]
    offer.d8.assert_not_called()
    assert Incident.get_by_id(offer.incident.id).state == "triggered"


@pytest.mark.parametrize("setting", ["D8_LEAD_RESPONSE_URL", "INTERNAL_NOTIFY_KEY"])
def test_without_the_setting_nothing_is_sent(client, offer, app, setting):
    app.config[setting] = ""

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.status_code == 503
    assert "isn't set up" in res.get_json()["error"]
    offer.d8.assert_not_called()
    assert Incident.get_by_id(offer.incident.id).state == "triggered"


def test_an_outage_cannot_be_accepted(client, offer):
    oncall = offer.leads.oncall
    res = client.post(
        "/api/v1/integrations/uptime-kuma", json=_kuma(0), headers=KUMA_HEADERS
    )
    incident = Incident.get_by_id(res.get_json()["incident_id"])

    assert _answer(client, incident, user=oncall.eng).status_code == 400
    assert _answer(client, SimpleNamespace(id=99999)).status_code == 404
    offer.d8.assert_not_called()


def test_an_offer_d8_already_closed_is_not_sent(client, offer):
    _resolve_page(client, key=OFFER_KEY, reason="expired")

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.status_code == 200
    body = res.get_json()
    assert (body["ok"], body["stopped"], body["code"]) == (False, True, "offer_closed")
    assert body["message"] == "This offer has already closed (Expired in D8)."
    offer.d8.assert_not_called()


def test_the_card_gets_the_outcome_as_an_htmx_event(client, offer):
    res = _answer(client, offer.incident, user=offer.leads.sam, htmx=True)

    assert res.status_code == 200 and res.get_data() == b""
    event = json.loads(res.headers["HX-Trigger"])["lead-response"]
    assert (event["ok"], event["status"], event["lead_url"]) == (
        True,
        "accepted",
        LEAD_URL,
    )


def test_the_card_gets_a_failure_as_plain_text_for_the_error_toast(client, offer):
    offer.d8.side_effect = lead_response.requests.ConnectionError("refused")

    res = _answer(client, offer.incident, user=offer.leads.sam, htmx=True)

    assert res.status_code == 502
    assert res.get_data(as_text=True) == "Couldn't reach D8. Try again."
    assert "HX-Trigger" not in res.headers


def test_a_failed_local_resolve_still_reports_the_answer(client, offer, mocker):
    mocker.patch.object(
        oncall_service, "resolve_incident", side_effect=RuntimeError("db gone")
    )

    res = _answer(client, offer.incident, user=offer.leads.sam)

    assert res.status_code == 200 and res.get_json()["ok"] is True
