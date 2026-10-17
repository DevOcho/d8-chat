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
from app.services import oncall_service

# Wednesday 2026-10-14 11:00 in Santo Domingo (UTC-4, no DST). The current
# shift started Monday 2026-10-12 08:00 local = 12:00 UTC.
NOW = datetime.datetime(2026, 10, 14, 15, 0)
SHIFT_START = datetime.datetime(2026, 10, 12, 12, 0)
MINUTE = datetime.timedelta(minutes=1)
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


def test_user_in_chain(oncall):
    incident = _trigger(oncall)
    outsider = _user("outsider")
    for user in (oncall.eng, oncall.lead, oncall.director, oncall.ceo):
        assert oncall_service.user_in_chain(incident, user)
    assert not oncall_service.user_in_chain(incident, outsider)


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
