"""The Chief of Staff bot: D8's DMs (internal notify ``to``) and their 90-day
retention (app/services/chief_of_staff.py)."""

from datetime import timedelta

import pytest

from app.models import (
    Conversation,
    Message,
    User,
    UserConversationStatus,
    Workspace,
    utc_now,
)
from app.services import chief_of_staff, oncall_service

KEY = {"X-Internal-Key": "test-internal-notify-key"}


@pytest.fixture
def bot():
    return chief_of_staff.ensure_bot(Workspace.get(Workspace.name == "DevOcho"))


def _person(username="ana", email="Ana@Example.com", active=True):
    return User.create(
        username=username, email=email, display_name=username.title(), is_active=active
    )


def _dm(client, to, message="Buenos días"):
    return client.post(
        "/api/v1/internal/notify", json={"to": to, "message": message}, headers=KEY
    )


def test_a_dm_goes_to_the_person_with_that_email(client, bot):
    ana = _person()

    res = _dm(client, "ana@example.COM", "**Buenos días, Ana.**")

    assert res.status_code == 200 and res.get_json() == {"ok": True}
    ids = sorted([bot.id, ana.id])
    conv = Conversation.get(conversation_id_str=f"dm_{ids[0]}_{ids[1]}")
    msg = Message.get(Message.conversation == conv)
    assert msg.user_id == bot.id and msg.content == "**Buenos días, Ana.**"
    # Both sides have a status row, so it shows in Ana's sidebar and GET /dms.
    assert (
        UserConversationStatus.select()
        .where(UserConversationStatus.conversation == conv)
        .count()
        == 2
    )

    assert _dm(client, "ana@example.com").status_code == 200
    assert Message.select().where(Message.conversation == conv).count() == 2


def test_a_dm_to_nobody_known_is_a_404(client, bot):
    _person("gone", "gone@example.com", active=False)

    assert _dm(client, "nobody@example.com").status_code == 404
    assert _dm(client, "gone@example.com").status_code == 404
    assert Message.select().count() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"to": "not-an-email", "message": "hi"},
        {"to": "ana@example.com", "message": "   "},
        {"to": "ana@example.com"},
        {"to": "ana@example.com", "message": "hi", "page": {"key": "k", "title": "t"}},
        {"to": "ana@example.com", "message": "hi", "channel_name": "general"},
    ],
)
def test_a_bad_dm_request_is_a_400(client, bot, body):
    _person()
    res = client.post("/api/v1/internal/notify", json=body, headers=KEY)
    assert res.status_code == 400


def test_a_dm_needs_the_shared_secret(client, bot):
    _person()
    res = client.post(
        "/api/v1/internal/notify",
        json={"to": "ana@example.com", "message": "hi"},
        headers={"X-Internal-Key": "wrong"},
    )
    assert res.status_code == 401


def test_a_dm_without_the_bot_is_a_500(client):
    _person()
    assert _dm(client, "ana@example.com").status_code == 500


def _post(conversation, user, age_days, **fields):
    return Message.create(
        conversation=conversation,
        user=user,
        content=fields.pop("content", "x"),
        created_at=utc_now() - timedelta(days=age_days),
        **fields,
    )


def test_retention_deletes_the_bots_old_dms_and_nothing_else(client, bot):
    from app.services import chat_service

    ana = _person()
    bo = _person("bo", "bo@example.com")
    _conv_id, bot_dm, _ = chat_service.open_dm(bot, ana)
    _other_id, people_dm, _ = chat_service.open_dm(ana, bo)

    old_brief = _post(bot_dm, bot, 91)
    old_reply = _post(bot_dm, ana, 91)
    recent = _post(bot_dm, bot, 10)
    their_own = _post(people_dm, ana, 200)

    assert chief_of_staff.purge_old_messages() == 2
    left = {m.id for m in Message.select()}
    assert left == {recent.id, their_own.id}
    assert old_brief.id not in left and old_reply.id not in left


def test_a_thread_with_recent_replies_keeps_an_empty_placeholder(client, bot):
    from app.services import chat_service

    ana = _person()
    _conv_id, bot_dm, _ = chat_service.open_dm(bot, ana)
    starter = _post(bot_dm, bot, 95, content="names in here")
    _post(bot_dm, ana, 5, parent_message=starter, reply_type="thread")

    assert chief_of_staff.purge_old_messages() == 1
    placeholder = Message.get_by_id(starter.id)
    assert placeholder.is_deleted and placeholder.content == ""
    assert chief_of_staff.purge_old_messages() == 0  # placeholders are done


def test_retention_runs_at_most_hourly_per_worker(client, bot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        chief_of_staff, "purge_old_messages", lambda now=None: calls.append(now) or 0
    )
    monkeypatch.setitem(chief_of_staff._last_purge, "at", None)
    now = utc_now()

    chief_of_staff.purge_if_due(now)
    chief_of_staff.purge_if_due(now + timedelta(minutes=30))
    chief_of_staff.purge_if_due(now + timedelta(minutes=61))
    assert len(calls) == 2


def test_the_on_call_ticker_runs_retention(app, monkeypatch):
    ran = []
    monkeypatch.setattr(oncall_service, "run_escalations", lambda now: None)
    monkeypatch.setattr(oncall_service, "resend_pages", lambda now: None)
    monkeypatch.setattr(chief_of_staff, "purge_if_due", lambda now: ran.append(now))
    with app.test_request_context("/"):
        oncall_service.tick()
    assert len(ran) == 1
