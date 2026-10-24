# tests/test_render_performance.py
"""Guards for the hot paths' query counts and caching.

Opening a 30-message channel ran ~220 queries (about 8 per rendered message),
an @channel send in a 200-member channel ~800 before its broadcast, and the
notification fan-out ~7 per online member. These tests pin the batched
versions so a template or helper change can't quietly bring the N+1s back.
"""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from peewee import IntegrityError

from app import background
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    Poll,
    PollOption,
    Reaction,
    UploadedFile,
    User,
    UserConversationStatus,
    db,
    utc_now,
)
from app.routes import prepare_messages_for_render
from app.services import chat_service, minio_service


@contextmanager
def count_queries():
    counter = {"n": 0}
    original = db.obj.execute_sql

    def counting(sql, *args, **kwargs):
        counter["n"] += 1
        return original(sql, *args, **kwargs)

    db.obj.execute_sql = counting
    try:
        yield counter
    finally:
        db.obj.execute_sql = original


@pytest.fixture
def busy_channel(app):
    """#general with 20 members and 30 messages from different authors, with
    avatars, mentions, a #hashtag, a quote, a poll and a thread."""
    me = User.get_by_id(1)
    channel = Channel.get(name="general")
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    ChannelMember.create(user=me, channel=channel)
    users = []
    for i in range(2, 22):
        avatar = UploadedFile.create(
            uploader=me,
            original_filename=f"a{i}.png",
            stored_filename=f"a{i}.png",
            mime_type="image/png",
            file_size_bytes=1,
        )
        user = User.create(id=i, username=f"u{i}", email=f"u{i}@x.com", avatar=avatar)
        ChannelMember.create(user=user, channel=channel)
        users.append(user)
    start = utc_now() - datetime.timedelta(hours=2)
    messages = []
    for i in range(30):
        author = users[i % len(users)]
        messages.append(
            Message.create(
                user=author,
                conversation=conv,
                content=f"hi @u{2 + (i + 1) % 20} about #general and #topic{i % 3}",
                created_at=start + datetime.timedelta(minutes=i),
            )
        )
    messages[5].quoted_message = messages[2]
    messages[5].save()
    poll = Poll.create(message=messages[7], question="q?")
    PollOption.create(poll=poll, text="a")
    Message.create(
        user=users[0],
        conversation=conv,
        content="in thread",
        parent_message=messages[9],
        reply_type="thread",
        created_at=start + datetime.timedelta(minutes=9, seconds=30),
    )
    return {
        "me": me,
        "channel": channel,
        "conv": conv,
        "users": users,
        "messages": messages,
    }


def test_opening_a_busy_channel_runs_a_bounded_number_of_queries(
    logged_in_client, busy_channel
):
    with count_queries() as q:
        res = logged_in_client.get(f"/chat/channel/{busy_channel['channel'].id}")
    assert res.status_code == 200
    # ~220 before. Now each query runs once whatever the page holds (29 for
    # this route today: membership, read state, member count, the page, and
    # one batch each for authors, quotes, polls, reply counts, reactions,
    # attachments, @names and #channels).
    assert q["n"] <= 35, q["n"]


def test_prepared_messages_render_without_further_queries(app, busy_channel):
    messages = list(Message.select().where(Message.in_timeline()))
    reactions_map, attachments_map = prepare_messages_for_render(messages)
    with count_queries() as q:
        for m in messages:
            assert m.user.username
            assert m.conversation.type == "channel"
            _ = m.user.avatar_url
            _ = m.poll_obj
            _ = m.thread_reply_count
            if m.quoted_message_id:
                assert m.quoted_message.user.username
    assert q["n"] == 0
    assert messages[9].thread_reply_count == 1
    assert messages[7].poll_obj is not None
    assert messages[0].poll_obj is None


def test_at_channel_creates_mentions_in_one_statement(app, busy_channel, mocker):
    mocker.patch("app.services.chat_service.chat_manager")
    sender = busy_channel["users"][0]
    with count_queries() as q:
        msg = chat_service.handle_new_message(
            sender=sender,
            conversation=busy_channel["conv"],
            chat_text="@channel heads up",
        )
    assert (
        Mention.select().where(Mention.message == msg).count() == 20
    )  # all but sender
    assert q["n"] <= 15, q["n"]


def test_at_channel_and_direct_mention_overlap_once(app, busy_channel, mocker):
    mocker.patch("app.services.chat_service.chat_manager")
    sender = busy_channel["users"][0]
    msg = chat_service.handle_new_message(
        sender=sender,
        conversation=busy_channel["conv"],
        chat_text="@channel and especially @u3",
    )
    assert (
        Mention.select().where((Mention.message == msg) & (Mention.user == 3)).count()
        == 1
    )


def test_fan_out_query_count_does_not_grow_per_member(app, busy_channel, mocker):
    mock_cm = mocker.patch("app.services.chat_service.chat_manager")
    mock_cm.is_user_online_in_cluster.return_value = True
    mock_cm.is_user_active.return_value = True
    sender = busy_channel["users"][0]
    msg = chat_service.handle_new_message(
        sender=sender, conversation=busy_channel["conv"], chat_text="plain message"
    )
    with app.test_request_context("/"):
        with count_queries() as q:
            chat_service.send_notifications_for_new_message(msg, sender)
    # 20 online recipients; ~150 before.
    assert q["n"] <= 15, q["n"]
    # Every recipient got a status row and one bold-link frame.
    assert (
        UserConversationStatus.select()
        .where(UserConversationStatus.conversation == busy_channel["conv"])
        .count()
        == 20
    )
    frames = [
        c for c in mock_cm.send_to_user.call_args_list if "_raw_html" in c.args[1]
    ]
    assert len(frames) == 20


def test_unread_threads_check_is_one_query(logged_in_client, busy_channel):
    from flask import g

    from app.routes import _has_unread_threads

    me = busy_channel["me"]
    parent = busy_channel["messages"][3]
    Message.create(
        user=me,
        conversation=busy_channel["conv"],
        content="mine",
        parent_message=parent,
        reply_type="thread",
    )
    Message.update(last_reply_at=utc_now()).where(Message.id == parent.id).execute()
    with logged_in_client.application.test_request_context("/"):
        g.user = me
        with count_queries() as q:
            assert (
                _has_unread_threads(utc_now() - datetime.timedelta(minutes=1)) is True
            )
        assert q["n"] == 1
        assert _has_unread_threads(utc_now() + datetime.timedelta(minutes=1)) is False


def test_presigned_urls_are_stable_within_a_window(app):
    with app.app_context():
        first = minio_service.get_presigned_url("photo.png")
        second = minio_service.get_presigned_url("photo.png")
    assert first and first == second


def test_signing_window_leaves_at_least_15_minutes(app):
    now = datetime.datetime(2026, 10, 24, 10, 29, 59, tzinfo=datetime.timezone.utc)
    start = minio_service._signing_window_start(now)
    assert start == datetime.datetime(2026, 10, 24, 10, 0, tzinfo=datetime.timezone.utc)
    remaining = start + minio_service.URL_LIFETIME - now
    assert remaining >= datetime.timedelta(minutes=15)


def test_duplicate_channel_membership_is_rejected(app, busy_channel):
    with pytest.raises(IntegrityError):
        with db.atomic():
            ChannelMember.create(
                user=busy_channel["me"], channel=busy_channel["channel"]
            )


def test_double_reaction_is_not_an_error(logged_in_client, busy_channel):
    msg = busy_channel["messages"][0]
    Reaction.create(user=busy_channel["me"], message=msg, emoji="👍")
    # The toggle sees no reaction (a racing request is about to add it)...
    with patch("app.blueprints.messages.Reaction.get_or_none", return_value=None):
        res = logged_in_client.post(
            f"/chat/message/{msg.id}/react", data={"emoji": "👍"}
        )
    assert res.status_code == 200
    assert Reaction.select().where(Reaction.message == msg).count() == 1


def test_background_work_is_bounded(app):
    seen = []

    def task():
        seen.append(background._slots._value)

    background._run_in_context(app, task, (), {})
    assert seen == [background.MAX_CONCURRENT - 1]
    assert background._slots._value == background.MAX_CONCURRENT


def test_pool_waits_for_a_free_connection(app, mocker):
    from app import models

    connect = mocker.patch("app.models.connect")
    mocker.patch("app.models.db")
    fake_app = type("A", (), {"config": {"DATABASE_URI": "postgresql://u:p@h/d"}})()
    models.initialize_db(fake_app)
    assert connect.call_args.kwargs["timeout"] == 10
