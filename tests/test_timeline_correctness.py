# tests/test_timeline_correctness.py
"""Correctness of what the timeline, Unreads and the sidebar show.

- Thread replies live in the thread panel: they must not take a slot in the
  30-message page, appear inline in older pages or the reconnect backfill, or
  light a conversation up as unread.
- Leaving or being removed from a channel ends its read-state row and its
  live subscription, so Unreads stops feeding the ex-member its messages.
- Deleting a message that starts a thread leaves a placeholder instead of
  orphaning the replies.
- Hot rows are updated column-by-column so concurrent writes aren't reverted,
  and a Valkey failure after the commit doesn't turn a send into a 500.
"""

import datetime
import json
from unittest.mock import Mock

import pytest

from app.blueprints.api_v1 import generate_api_token
from app.chat_manager import ChatManager
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    Reaction,
    User,
    UserConversationStatus,
    utc_now,
)
from app.routes import PAGE_SIZE
from app.services import chat_service


@pytest.fixture
def general(app):
    me = User.get_by_id(1)
    other = User.create(id=2, username="other", email="o@x.com")
    channel = Channel.get(name="general")
    ChannelMember.create(user=me, channel=channel)
    ChannelMember.create(user=other, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    return {"me": me, "other": other, "channel": channel, "conv": conv}


def _thread(conv, author, count, start=None):
    start = start or utc_now() - datetime.timedelta(hours=1)
    parent = Message.create(
        user=author, conversation=conv, content="parent", created_at=start
    )
    replies = [
        Message.create(
            user=author,
            conversation=conv,
            content=f"reply {i}",
            parent_message=parent,
            reply_type="thread",
            created_at=start + datetime.timedelta(seconds=i + 1),
        )
        for i in range(count)
    ]
    return parent, replies


# --- Thread replies stay out of the timeline -----------------------------------


def test_busy_thread_does_not_crowd_out_the_channel_page(logged_in_client, general):
    parent, _ = _thread(general["conv"], general["other"], PAGE_SIZE + 5)
    html = logged_in_client.get(f"/chat/channel/{general['channel'].id}").get_data(
        as_text=True
    )
    assert f'id="message-{parent.id}"' in html


def test_older_page_skips_thread_replies(logged_in_client, general):
    parent, replies = _thread(general["conv"], general["other"], 3)
    newest = Message.create(
        user=general["other"], conversation=general["conv"], content="new"
    )
    res = logged_in_client.get(
        f"/chat/messages/{general['conv'].conversation_id_str}?before_message_id={newest.id}"
    )
    html = res.get_data(as_text=True)
    assert f'id="message-{parent.id}"' in html
    for reply in replies:
        assert f'id="message-{reply.id}"' not in html


def test_reconnect_backfill_skips_thread_replies(logged_in_client, general):
    parent, replies = _thread(general["conv"], general["other"], 2)
    res = logged_in_client.get(
        f"/chat/conversations/{general['conv'].conversation_id_str}/messages/since/0"
    )
    html = res.get_data(as_text=True)
    assert f'id="message-{parent.id}"' in html
    assert f'id="message-{replies[0].id}"' not in html


def test_thread_reply_alone_does_not_make_a_channel_unread(logged_in_client, general):
    old = utc_now() - datetime.timedelta(days=1)
    parent = Message.create(
        user=general["other"], conversation=general["conv"], content="p", created_at=old
    )
    UserConversationStatus.create(
        user=general["me"], conversation=general["conv"], last_read_timestamp=utc_now()
    )
    Message.create(
        user=general["other"],
        conversation=general["conv"],
        content="in thread",
        parent_message=parent,
        reply_type="thread",
        created_at=utc_now() + datetime.timedelta(seconds=5),
    )
    html = logged_in_client.get("/chat").get_data(as_text=True)
    link = html.split(f'id="link-{general["conv"].conversation_id_str}"', 1)[1][:120]
    assert "fw-bold" not in link
    unreads = logged_in_client.get("/chat/unreads").get_data(as_text=True)
    assert "in thread" not in unreads


def test_jump_to_a_thread_reply_shows_its_parent_in_context(logged_in_client, general):
    parent, replies = _thread(general["conv"], general["other"], 1)
    html = logged_in_client.get(f"/chat/message/{replies[0].id}/context").get_data(
        as_text=True
    )
    assert f'id="message-{parent.id}"' in html
    assert f'id="message-{replies[0].id}"' not in html


# --- Leaving a channel ------------------------------------------------------------


def test_left_channel_stops_feeding_unreads(logged_in_client, general):
    UserConversationStatus.create(
        user=general["me"],
        conversation=general["conv"],
        last_read_timestamp=utc_now() - datetime.timedelta(days=1),
    )
    res = logged_in_client.post(f"/chat/channel/{general['channel'].id}/leave")
    assert res.status_code in (200, 204, 302)
    assert (
        UserConversationStatus.get_or_none(
            user=general["me"], conversation=general["conv"]
        )
        is None
    )
    Message.create(
        user=general["other"], conversation=general["conv"], content="after I left"
    )
    assert "after I left" not in logged_in_client.get("/chat/unreads").get_data(
        as_text=True
    )


def test_stale_status_row_of_an_ex_member_is_ignored(logged_in_client, general):
    """Rows left behind before leaving cleaned them up."""
    ChannelMember.delete().where(ChannelMember.user == general["me"]).execute()
    UserConversationStatus.create(
        user=general["me"],
        conversation=general["conv"],
        last_read_timestamp=utc_now() - datetime.timedelta(days=1),
    )
    Message.create(
        user=general["other"], conversation=general["conv"], content="private-ish"
    )
    assert "private-ish" not in logged_in_client.get("/chat/unreads").get_data(
        as_text=True
    )


def test_end_channel_membership_unsubscribes_everywhere(app, general, mocker):
    publish = mocker.patch("app.services.chat_service.chat_manager")
    chat_service.end_channel_membership(general["me"].id, general["channel"])
    assert (
        not ChannelMember.select().where(ChannelMember.user == general["me"]).exists()
    )
    publish.unsubscribe_user_everywhere.assert_called_once_with(
        general["me"].id, general["conv"].conversation_id_str
    )


def test_unsubscribe_control_frame_stops_only_that_conversation(app):
    manager = ChatManager()
    following, elsewhere = Mock(), Mock()
    following.channel_id, elsewhere.channel_id = "channel_7", "channel_8"
    following.user = elsewhere.user = None
    manager.all_clients = {5: {following, elsewhere}}
    with app.app_context():
        manager._dispatch(
            {"channel": b"user:5", "data": json.dumps({"_unsubscribe": "channel_7"})}
        )
    assert following.channel_id is None
    assert elsewhere.channel_id == "channel_8"
    following.send.assert_not_called()


def test_membership_changes_reach_every_worker(logged_in_client, general, mocker):
    send = mocker.patch("app.blueprints.channels.chat_manager.send_to_user")
    ChannelMember.update(role="admin").where(
        ChannelMember.user == general["me"]
    ).execute()
    third = User.create(id=3, username="third", email="t@x.com")
    logged_in_client.post(
        f"/chat/channel/{general['channel'].id}/members",
        data={"user_id": third.id},
    )
    assert any(c.args[0] == third.id for c in send.call_args_list)


# --- Deleting a thread parent ------------------------------------------------------


def test_deleting_a_thread_parent_leaves_a_placeholder(logged_in_client, general):
    parent, replies = _thread(general["conv"], general["me"], 2)
    Reaction.create(user=general["other"], message=parent, emoji="👍")
    assert logged_in_client.delete(f"/chat/message/{parent.id}").status_code == 204

    kept = Message.get_by_id(parent.id)
    assert kept.is_deleted and kept.content == ""
    assert Reaction.select().where(Reaction.message == parent).count() == 0
    assert all(Message.get_by_id(r.id).parent_message_id == parent.id for r in replies)

    html = logged_in_client.get(f"/chat/thread/{parent.id}").get_data(as_text=True)
    assert "This message was deleted." in html
    assert "reply 0" in html


def test_deleting_the_last_reply_removes_the_placeholder(logged_in_client, general):
    parent, replies = _thread(general["conv"], general["me"], 1)
    logged_in_client.delete(f"/chat/message/{parent.id}")
    logged_in_client.delete(f"/chat/message/{replies[0].id}")
    assert Message.get_or_none(id=parent.id) is None


def test_a_message_without_replies_is_deleted_outright(logged_in_client, general):
    msg = Message.create(
        user=general["me"], conversation=general["conv"], content="bye"
    )
    logged_in_client.delete(f"/chat/message/{msg.id}")
    assert Message.get_or_none(id=msg.id) is None


def test_placeholder_cannot_be_edited(logged_in_client, client, general, app):
    parent, _ = _thread(general["conv"], general["me"], 1)
    logged_in_client.delete(f"/chat/message/{parent.id}")
    res = logged_in_client.put(f"/chat/message/{parent.id}", data={"content": "back"})
    assert res.status_code == 400
    with app.app_context():
        token = generate_api_token(1)
    res = client.patch(
        f"/api/v1/messages/{parent.id}",
        headers={"Authorization": f"Bearer d8_sec_{token}"},
        json={"content": "back"},
    )
    assert res.status_code == 400
    assert Message.get_by_id(parent.id).content == ""


# --- Edits --------------------------------------------------------------------------


def test_empty_edit_is_a_400_not_a_500(logged_in_client, general):
    msg = Message.create(user=general["me"], conversation=general["conv"], content="hi")
    res = logged_in_client.put(f"/chat/message/{msg.id}", data={"content": "   "})
    assert res.status_code == 400
    assert Message.get_by_id(msg.id).content == "hi"


def test_api_edit_rejects_non_string_content(client, general, app):
    msg = Message.create(user=general["me"], conversation=general["conv"], content="hi")
    with app.app_context():
        token = generate_api_token(1)
    res = client.patch(
        f"/api/v1/messages/{msg.id}",
        headers={"Authorization": f"Bearer d8_sec_{token}"},
        json={"content": 42},
    )
    assert res.status_code == 400


def test_edit_does_not_revert_a_concurrent_thread_reply(logged_in_client, general):
    msg = Message.create(user=general["me"], conversation=general["conv"], content="hi")
    stamp = utc_now()
    # A reply lands (bumping last_reply_at) while the edit request is in flight.
    original_get = Message.get_or_none

    def get_then_reply(*args, **kwargs):
        found = original_get(*args, **kwargs)
        Message.update(last_reply_at=stamp).where(Message.id == msg.id).execute()
        return found

    import app.blueprints.messages as messages_bp

    messages_bp.Message.get_or_none = get_then_reply
    try:
        logged_in_client.put(f"/chat/message/{msg.id}", data={"content": "edited"})
    finally:
        messages_bp.Message.get_or_none = original_get
    fresh = Message.get_by_id(msg.id)
    assert fresh.content == "edited"
    assert fresh.last_reply_at == stamp


def test_quote_reply_and_edit_composers_submit_attachments(logged_in_client, general):
    msg = Message.create(user=general["me"], conversation=general["conv"], content="hi")
    for path in (
        f"/chat/message/{msg.id}/reply",
        f"/chat/message/{msg.id}/load_for_edit",
    ):
        html = logged_in_client.get(path).get_data(as_text=True)
        assert 'name="attachment_file_ids"' in html


# --- Concurrency and outages ---------------------------------------------------------


def test_notification_stamp_does_not_revert_a_read(app, general, mocker):
    conv = Conversation.create(conversation_id_str="dm_1_2", type="dm")
    before = utc_now() - datetime.timedelta(hours=1)
    UserConversationStatus.create(
        user=general["me"], conversation=conv, last_read_timestamp=before
    )
    msg = chat_service.handle_new_message(
        sender=general["other"], conversation=conv, chat_text="hi"
    )

    mock_cm = mocker.patch("app.services.chat_service.chat_manager")
    mock_cm.is_user_online_in_cluster.return_value = True
    read_at = utc_now() + datetime.timedelta(seconds=1)

    def read_meanwhile(*args, **kwargs):
        UserConversationStatus.update(last_read_timestamp=read_at).where(
            UserConversationStatus.user == general["me"]
        ).execute()

    mock_cm.send_to_user.side_effect = read_meanwhile
    with app.test_request_context("/"):
        chat_service.send_notifications_for_new_message(msg, general["other"])

    status = UserConversationStatus.get(user=general["me"], conversation=conv)
    assert status.last_read_timestamp == read_at
    assert status.last_notified_timestamp is not None


def test_valkey_outage_does_not_fail_a_send(logged_in_client, general, mocker):
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")
    from app.chat_manager import chat_manager

    chat_manager.redis_client.publish.side_effect = ConnectionError("valkey down")
    res = logged_in_client.post(
        f"/chat/conversations/{general['conv'].conversation_id_str}/messages",
        data={"chat_message": "still saved"},
    )
    assert res.status_code == 204
    assert Message.select().where(Message.content == "still saved").count() == 1


def test_publish_without_a_client_is_dropped(app):
    manager = ChatManager()
    with app.app_context():
        assert manager._publish("chat:x", {"a": 1}) is False


def test_mention_in_a_thread_still_badges(logged_in_client, general):
    parent = Message.create(
        user=general["other"], conversation=general["conv"], content="p"
    )
    UserConversationStatus.create(
        user=general["me"],
        conversation=general["conv"],
        last_read_timestamp=utc_now() - datetime.timedelta(minutes=5),
    )
    reply = Message.create(
        user=general["other"],
        conversation=general["conv"],
        content="@testuser look",
        parent_message=parent,
        reply_type="thread",
    )
    Mention.create(user=general["me"], message=reply)
    html = logged_in_client.get("/chat").get_data(as_text=True)
    badge = html.split(f'id="unread-badge-{general["conv"].conversation_id_str}"', 1)[
        1
    ][:120]
    assert "bg-danger" in badge
