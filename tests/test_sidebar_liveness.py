# tests/test_sidebar_liveness.py
"""Keeping the sidebar and the open pane live without a refresh.

- A DM the recipient "left" (which deletes their read-state row) must come
  back with a badge and a sidebar row when the other person writes again.
  The recreated row used to default its read time to *now*, after the
  message, so the message counted as read: only the sound fired.
- The WebSocket answers the web client's heartbeat ping and acknowledges a
  subscribe so the client can backfill the gap before it.
- Message-list appends carry their conversation id so the client can drop
  one meant for a pane that's no longer on screen.
"""

from unittest.mock import Mock

from app.blueprints.api_v1 import generate_api_token
from app.htmx_oob import message_list_append_oob
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    User,
    UserConversationStatus,
)
from app.routes import _process_ws_event
from app.services import chat_service


def _html_sent_to(mock_cm, user_id):
    parts = []
    for c in mock_cm.send_to_user.call_args_list:
        if c.args[0] == user_id and isinstance(c.args[1], dict):
            parts.append(c.args[1].get("_raw_html") or "")
    return "".join(parts)


def _online(mocker):
    mock_cm = mocker.patch("app.services.chat_service.chat_manager")
    mock_cm.is_user_online_in_cluster.return_value = True
    mock_cm.is_user_active.return_value = True
    return mock_cm


def test_left_dm_comes_back_with_a_row_and_badge(app, logged_in_client, mocker):
    me = User.get_by_id(1)
    other = User.create(id=2, username="other", email="o@test.com")
    logged_in_client.get(f"/chat/dm/{other.id}")
    logged_in_client.delete(f"/chat/dm/{other.id}/leave")
    conv = Conversation.get(conversation_id_str="dm_1_2")

    mock_cm = _online(mocker)
    msg = chat_service.handle_new_message(
        sender=other, conversation=conv, chat_text="you there?"
    )
    with app.test_request_context("/"):
        chat_service.send_notifications_for_new_message(msg, other)

    html = _html_sent_to(mock_cm, me.id)
    assert 'id="dm-item-dm_1_2"' in html
    assert 'id="unread-badge-dm_1_2"' in html
    status = UserConversationStatus.get(user=me, conversation=conv)
    assert status.last_read_timestamp < msg.created_at


def test_first_dm_to_someone_who_never_opened_it_is_unread(app, mocker):
    """No status row at all for the recipient (e.g. the DM was created by a
    path that only made the sender's)."""
    me = User.get_by_id(1)
    other = User.create(id=2, username="other", email="o@test.com")
    conv = Conversation.create(conversation_id_str="dm_1_2", type="dm")

    mock_cm = _online(mocker)
    msg = chat_service.handle_new_message(
        sender=other, conversation=conv, chat_text="hi"
    )
    with app.test_request_context("/"):
        chat_service.send_notifications_for_new_message(msg, other)

    assert 'id="dm-item-dm_1_2"' in _html_sent_to(mock_cm, me.id)
    # The sender's own row isn't backdated: they've read what they sent.
    sender_status = UserConversationStatus.get(user=other, conversation=conv)
    assert sender_status.last_read_timestamp >= msg.created_at


def test_new_channel_member_without_a_status_row_sees_the_channel_unread(app, mocker):
    me = User.get_by_id(1)
    other = User.create(id=2, username="other", email="o@test.com")
    channel = Channel.get(name="general")
    ChannelMember.create(user=me, channel=channel)
    ChannelMember.create(user=other, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    mock_cm = _online(mocker)
    msg = chat_service.handle_new_message(
        sender=other, conversation=conv, chat_text="yo"
    )
    with app.test_request_context("/"):
        chat_service.send_notifications_for_new_message(msg, other)

    assert f'id="link-channel_{channel.id}"' in _html_sent_to(mock_cm, me.id)


def _ws(user_id=1, api=False):
    ws = Mock()
    ws.user = User.get_by_id(user_id)
    ws.is_api_client = api
    ws.channel_id = None
    return ws


def test_ping_gets_a_pong(app, mocker):
    send = mocker.patch("app.routes.chat_manager.send_direct")
    ws = _ws()
    _process_ws_event(ws, {"type": "ping"})
    send.assert_called_once_with(ws, {"type": "pong"})


def test_ping_is_not_treated_as_a_message(app, mocker):
    mocker.patch("app.routes.chat_manager.send_direct")
    inbound = mocker.patch("app.routes.handle_inbound_message")
    _process_ws_event(_ws(), {"type": "ping", "conversation_id": "channel_1"})
    inbound.assert_not_called()


def test_web_subscribe_is_acknowledged(app, mocker):
    send = mocker.patch("app.routes.chat_manager.send_direct")
    channel = Channel.get(name="general")
    ChannelMember.create(user=User.get_by_id(1), channel=channel)
    ws = _ws()
    conv_id = f"channel_{channel.id}"
    _process_ws_event(ws, {"type": "subscribe", "conversation_id": conv_id})
    assert ws.channel_id == conv_id
    send.assert_called_once_with(ws, {"type": "subscribed", "conversation_id": conv_id})


def test_mobile_subscribe_is_not_acknowledged(app, mocker):
    send = mocker.patch("app.routes.chat_manager.send_direct")
    channel = Channel.get(name="general")
    ChannelMember.create(user=User.get_by_id(1), channel=channel)
    ws = _ws(api=True)
    _process_ws_event(
        ws, {"type": "subscribe", "conversation_id": f"channel_{channel.id}"}
    )
    send.assert_not_called()


def test_refused_subscribe_is_not_acknowledged(app, mocker):
    send = mocker.patch("app.routes.chat_manager.send_direct")
    channel = Channel.get(name="general")  # user 1 is not a member
    _process_ws_event(
        _ws(), {"type": "subscribe", "conversation_id": f"channel_{channel.id}"}
    )
    send.assert_not_called()


def test_message_list_appends_are_tagged_with_their_conversation():
    html = message_list_append_oob("dm_1_2", "<p>x</p>")
    assert html == (
        '<div hx-swap-oob="beforeend:#message-list" '
        'data-conversation-id="dm_1_2"><p>x</p></div>'
    )


def test_live_broadcast_and_catch_up_are_tagged(app, logged_in_client, mocker):
    channel = Channel.get(name="general")
    ChannelMember.create(user=User.get_by_id(1), channel=channel)
    conv_id = f"channel_{channel.id}"
    broadcast = mocker.patch("app.routes.chat_manager.broadcast")
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

    res = logged_in_client.post(
        f"/chat/conversations/{conv_id}/messages", data={"chat_message": "hi"}
    )
    assert res.status_code == 204
    payload = broadcast.call_args.args[1]
    assert f'data-conversation-id="{conv_id}"' in payload["_raw_html"]

    first = Message.select().order_by(Message.id).first()
    catch_up = logged_in_client.get(
        f"/chat/conversations/{conv_id}/messages/since/{first.id - 1}"
    )
    assert f'data-conversation-id="{conv_id}"' in catch_up.get_data(as_text=True)


def test_api_poll_respects_admin_only_posting(client, app):
    channel = Channel.get(name="announcements")
    channel.posting_restricted_to_admins = True
    channel.save()
    ChannelMember.create(user=User.get_by_id(1), channel=channel)
    with app.app_context():
        token = generate_api_token(1)
    res = client.post(
        f"/api/v1/conversations/channel_{channel.id}/polls",
        headers={"Authorization": f"Bearer d8_sec_{token}"},
        json={"question": "q", "options": ["a", "b"]},
    )
    assert res.status_code == 403
