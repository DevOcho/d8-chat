"""Tests for the push notification subsystem.

Covers three concerns:
 1. The /api/v1/users/me/devices endpoints (register / upsert / deregister).
 2. ``push_service.send_to_user`` in isolation — happy path, soft errors,
    stale-token cleanup.
 3. The chat_service dispatch wiring — DMs / mentions / thread replies
    push to offline recipients but skip online ones and never the sender.

``push_service.is_configured()`` returns False under TestConfig
(FIREBASE_CREDENTIALS_PATH is unset), so trigger tests need to monkeypatch
the public API of the service rather than firebase_admin internals. That
keeps the test surface stable across firebase-admin versions.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.chat_manager import chat_manager
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    DeviceToken,
    Message,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services import chat_service, push_service


def _login(client, user_id=1, password="password123"):
    """Set a password on the seeded user and grab an API token."""
    user = User.get_by_id(user_id)
    user.set_password(password)
    user.save()
    res = client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": password},
    )
    assert res.status_code == 200, res.get_json()
    return res.get_json()["api_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Endpoint: POST/DELETE /api/v1/users/me/devices
# ---------------------------------------------------------------------------


def test_register_device_creates_row(client):
    token = _login(client)
    res = client.post(
        "/api/v1/users/me/devices",
        json={"platform": "android", "token": "fcm-abc-123"},
        headers=_auth(token),
    )
    assert res.status_code == 204

    row = DeviceToken.get(DeviceToken.token == "fcm-abc-123")
    assert row.user_id == 1
    assert row.platform == "android"
    assert row.last_used_at is not None


def test_register_device_idempotent_refresh(client):
    """Re-registering the same token under the same user is a refresh, not a duplicate."""
    token = _login(client)
    body = {"platform": "ios", "token": "fcm-refresh"}
    client.post("/api/v1/users/me/devices", json=body, headers=_auth(token))
    client.post("/api/v1/users/me/devices", json=body, headers=_auth(token))

    assert DeviceToken.select().where(DeviceToken.token == "fcm-refresh").count() == 1


def test_register_device_reassigns_across_users(client):
    """A reprovisioned device that re-registers under a new user reassigns the row."""
    # Seed a second user and grab their token.
    other = User.create(
        id=2, username="alice", email="alice@example.com", display_name="Alice"
    )
    ws = Workspace.get(Workspace.name == "DevOcho")
    WorkspaceMember.create(user=other, workspace=ws)
    other.set_password("alicepw1")
    other.save()

    token_1 = _login(client, user_id=1)
    client.post(
        "/api/v1/users/me/devices",
        json={"platform": "android", "token": "shared-device"},
        headers=_auth(token_1),
    )
    assert DeviceToken.get(DeviceToken.token == "shared-device").user_id == 1

    token_2 = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "alicepw1"}
    ).get_json()["api_token"]
    client.post(
        "/api/v1/users/me/devices",
        json={"platform": "android", "token": "shared-device"},
        headers=_auth(token_2),
    )
    row = DeviceToken.get(DeviceToken.token == "shared-device")
    assert row.user_id == other.id
    # And there's still exactly one row for that token.
    assert DeviceToken.select().where(DeviceToken.token == "shared-device").count() == 1


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, 400),
        ({"platform": "android"}, 400),
        ({"token": "x"}, 400),
        ({"platform": "windows", "token": "x"}, 400),
        ({"platform": "android", "token": "   "}, 400),
        ({"platform": "android", "token": "x" * 5000}, 400),
    ],
)
def test_register_device_rejects_bad_input(client, body, expected):
    token = _login(client)
    res = client.post("/api/v1/users/me/devices", json=body, headers=_auth(token))
    assert res.status_code == expected


def test_unregister_device_removes_row(client):
    token = _login(client)
    client.post(
        "/api/v1/users/me/devices",
        json={"platform": "ios", "token": "to-delete"},
        headers=_auth(token),
    )
    res = client.delete(
        "/api/v1/users/me/devices",
        json={"token": "to-delete"},
        headers=_auth(token),
    )
    assert res.status_code == 204
    assert DeviceToken.select().where(DeviceToken.token == "to-delete").count() == 0


def test_unregister_device_idempotent(client):
    """Deleting a nonexistent token is a no-op success."""
    token = _login(client)
    res = client.delete(
        "/api/v1/users/me/devices",
        json={"token": "never-existed"},
        headers=_auth(token),
    )
    assert res.status_code == 204


def test_unregister_device_only_owns_own_tokens(client):
    """User A cannot deregister User B's token by guessing the string."""
    other = User.create(
        id=2, username="bob", email="bob@example.com", display_name="Bob"
    )
    DeviceToken.create(user=other, platform="android", token="bobs-token")

    token = _login(client, user_id=1)
    res = client.delete(
        "/api/v1/users/me/devices",
        json={"token": "bobs-token"},
        headers=_auth(token),
    )
    assert res.status_code == 204
    # Token must still exist.
    assert DeviceToken.get(DeviceToken.token == "bobs-token").user_id == other.id


def test_devices_require_auth(client):
    res = client.post(
        "/api/v1/users/me/devices",
        json={"platform": "android", "token": "x"},
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# push_service.send_to_user
# ---------------------------------------------------------------------------


def _force_firebase_configured(monkeypatch, send_each_for_multicast):
    """Pretend firebase-admin is initialized; intercept the send call.

    We don't actually init firebase_admin (no credentials in tests) — we
    just flip the module-level sentinel and patch the SDK shim.
    """
    monkeypatch.setattr(push_service, "_firebase_app", object())

    fake_messaging = SimpleNamespace(
        MulticastMessage=lambda **kw: SimpleNamespace(**kw),
        Notification=lambda **kw: SimpleNamespace(**kw),
        send_each_for_multicast=send_each_for_multicast,
    )

    class _FakeFirebaseAdmin:
        messaging = fake_messaging

    # ``from firebase_admin import messaging`` inside send_to_user needs to
    # resolve to our fake. Inject a fake module into sys.modules.
    import sys

    fake_module = SimpleNamespace(messaging=fake_messaging)
    monkeypatch.setitem(sys.modules, "firebase_admin", fake_module)


def test_send_to_user_no_op_when_unconfigured(app, monkeypatch):
    """When _firebase_app is None, send_to_user does nothing — no DB lookup."""
    monkeypatch.setattr(push_service, "_firebase_app", None)
    with app.app_context():
        # No DeviceToken rows; would 500 if we tried to send.
        push_service.send_to_user(1, title="t", body="b")  # should not raise


def test_send_to_user_no_op_when_user_has_no_tokens(app, monkeypatch):
    sent = MagicMock()
    _force_firebase_configured(monkeypatch, sent)
    with app.app_context():
        push_service.send_to_user(1, title="t", body="b")
    assert sent.call_count == 0


def test_send_to_user_dispatches_and_marks_last_used(app, monkeypatch):
    user = User.get_by_id(1)
    DeviceToken.create(user=user, platform="android", token="good-token")

    def fake_send(message, app=None):
        return SimpleNamespace(
            responses=[SimpleNamespace(success=True, exception=None)]
        )

    sent = MagicMock(side_effect=fake_send)
    _force_firebase_configured(monkeypatch, sent)

    with app.app_context():
        push_service.send_to_user(user.id, title="Hi", body="Body", data={"k": "v"})

    sent.assert_called_once()
    sent_msg = sent.call_args.args[0]
    assert sent_msg.tokens == ["good-token"]
    assert sent_msg.notification.title == "Hi"
    assert sent_msg.data == {"k": "v"}

    row = DeviceToken.get(DeviceToken.token == "good-token")
    assert row.last_used_at is not None


def test_send_to_user_prunes_stale_tokens(app, monkeypatch):
    user = User.get_by_id(1)
    DeviceToken.create(user=user, platform="android", token="stale")
    DeviceToken.create(user=user, platform="android", token="alive")

    class StaleExc(Exception):
        code = "UNREGISTERED"

    def fake_send(message, app=None):
        return SimpleNamespace(
            responses=[
                SimpleNamespace(success=False, exception=StaleExc("gone")),
                SimpleNamespace(success=True, exception=None),
            ]
        )

    _force_firebase_configured(monkeypatch, MagicMock(side_effect=fake_send))

    with app.app_context():
        push_service.send_to_user(user.id, title="t", body="b")

    remaining = {row.token for row in DeviceToken.select()}
    assert remaining == {"alive"}


def test_send_to_user_keeps_token_on_unknown_error(app, monkeypatch):
    """Transient FCM errors must not prune the token (would lose delivery permanently)."""
    user = User.get_by_id(1)
    DeviceToken.create(user=user, platform="android", token="transient")

    class TransientExc(Exception):
        code = "INTERNAL"

    def fake_send(message, app=None):
        return SimpleNamespace(
            responses=[SimpleNamespace(success=False, exception=TransientExc("oops"))]
        )

    _force_firebase_configured(monkeypatch, MagicMock(side_effect=fake_send))
    with app.app_context():
        push_service.send_to_user(user.id, title="t", body="b")

    assert DeviceToken.select().where(DeviceToken.token == "transient").exists()


def test_send_to_user_swallows_send_exception(app, monkeypatch):
    """An exception raised by the SDK itself must not propagate."""
    user = User.get_by_id(1)
    DeviceToken.create(user=user, platform="android", token="x")

    _force_firebase_configured(
        monkeypatch, MagicMock(side_effect=RuntimeError("connection lost"))
    )
    with app.app_context():
        push_service.send_to_user(user.id, title="t", body="b")  # must not raise


# ---------------------------------------------------------------------------
# chat_service._dispatch_push_notifications wiring
# ---------------------------------------------------------------------------


def _make_dm(other_id=2):
    other = User.create(
        id=other_id,
        username=f"u{other_id}",
        email=f"u{other_id}@x.com",
        display_name=f"User {other_id}",
    )
    ws = Workspace.get(Workspace.name == "DevOcho")
    WorkspaceMember.create(user=other, workspace=ws)
    conv = Conversation.create(conversation_id_str=f"dm_1_{other_id}", type="dm")
    return other, conv


def _make_channel(name="random", members=()):
    ws = Workspace.get(Workspace.name == "DevOcho")
    channel = Channel.create(workspace=ws, name=name)
    conv = Conversation.create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )
    for u in members:
        ChannelMember.create(user=u, channel=channel)
    return channel, conv


def test_dispatch_no_op_when_push_not_configured(app, monkeypatch):
    """The whole push pass must short-circuit when Firebase isn't set up."""
    monkeypatch.setattr(push_service, "is_configured", lambda: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        other, conv = _make_dm()
        msg = Message.create(user=sender, conversation=conv, content="hi")
        chat_service.send_notifications_for_new_message(msg, sender)

    sent.assert_not_called()


def test_dm_pushes_to_offline_recipient(app, monkeypatch):
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        other, conv = _make_dm(other_id=42)
        msg = Message.create(user=sender, conversation=conv, content="hello")
        chat_service.send_notifications_for_new_message(msg, sender)

    sent.assert_called_once()
    kwargs = sent.call_args.kwargs
    assert sent.call_args.args[0] == other.id
    assert "Test User" in kwargs["title"]
    assert kwargs["data"]["conversation_id_str"] == conv.conversation_id_str
    assert kwargs["data"]["message_id"] == msg.id


def test_dm_skips_online_recipient(app, monkeypatch):
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: True)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        other, conv = _make_dm(other_id=43)
        msg = Message.create(user=sender, conversation=conv, content="hello")
        chat_service.send_notifications_for_new_message(msg, sender)

    sent.assert_not_called()


def test_sender_never_pushed_for_own_message(app, monkeypatch):
    """A self-DM (or any path that surfaces the sender) must not push the sender."""
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        # Self-DM: only the sender appears in user_ids.
        conv = Conversation.create(conversation_id_str="dm_1_1", type="dm")
        msg = Message.create(user=sender, conversation=conv, content="note to self")
        chat_service.send_notifications_for_new_message(msg, sender)

    sent.assert_not_called()


def test_mention_pushes_offline_user(app, monkeypatch):
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        mentioned = User.create(
            id=2,
            username="alice",
            email="alice@example.com",
            display_name="Alice",
        )
        ws = Workspace.get(Workspace.name == "DevOcho")
        WorkspaceMember.create(user=mentioned, workspace=ws)
        channel, conv = _make_channel("ops", members=[sender, mentioned])

        msg = chat_service.handle_new_message(sender, conv, "ping @alice please")
        chat_service.send_notifications_for_new_message(msg, sender)

    target_ids = [c.args[0] for c in sent.call_args_list]
    assert mentioned.id in target_ids


def test_thread_reply_pushes_prior_participants(app, monkeypatch):
    """Thread replies push to everyone who replied earlier AND the thread starter."""
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        starter = User.get_by_id(1)
        replier_a = User.create(id=2, username="a", email="a@x.com", display_name="A")
        replier_b = User.create(id=3, username="b", email="b@x.com", display_name="B")
        ws = Workspace.get(Workspace.name == "DevOcho")
        WorkspaceMember.create(user=replier_a, workspace=ws)
        WorkspaceMember.create(user=replier_b, workspace=ws)
        channel, conv = _make_channel(
            "threads", members=[starter, replier_a, replier_b]
        )

        parent = Message.create(user=starter, conversation=conv, content="topic")
        Message.create(
            user=replier_a,
            conversation=conv,
            content="r1",
            parent_message=parent,
            reply_type="thread",
        )
        # New reply from replier_b — should push to starter AND replier_a, but
        # not replier_b (the sender).
        new_reply = Message.create(
            user=replier_b,
            conversation=conv,
            content="r2",
            parent_message=parent,
            reply_type="thread",
        )
        chat_service.send_notifications_for_new_message(new_reply, replier_b)

    target_ids = {c.args[0] for c in sent.call_args_list}
    assert starter.id in target_ids
    assert replier_a.id in target_ids
    assert replier_b.id not in target_ids


def test_at_channel_does_not_push(app, monkeypatch):
    """@channel intentionally does NOT trigger push in v1."""
    monkeypatch.setattr(push_service, "is_configured", lambda: True)
    monkeypatch.setattr(chat_manager, "is_user_online_in_cluster", lambda uid: False)
    sent = MagicMock()
    monkeypatch.setattr(push_service, "send_to_user", sent)

    with app.app_context():
        sender = User.get_by_id(1)
        other = User.create(id=2, username="c", email="c@x.com", display_name="C")
        ws = Workspace.get(Workspace.name == "DevOcho")
        WorkspaceMember.create(user=other, workspace=ws)
        channel, conv = _make_channel("general2", members=[sender, other])

        msg = chat_service.handle_new_message(sender, conv, "heads up @channel")
        chat_service.send_notifications_for_new_message(msg, sender)

    sent.assert_not_called()
