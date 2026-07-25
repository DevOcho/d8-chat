"""Tests for date dividers on realtime and catch-up message appends.

The page templates emit a "Today"/"Yesterday"/"July 16th" divider whenever a
row's calendar day differs from the row above it. Realtime broadcasts append one
message at a time, so the divider has to be computed server-side against the
message that sits above it — otherwise the first message of a new day only grows
its divider on the next full page reload.
"""

import datetime

import pytest

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    User,
    Workspace,
    WorkspaceMember,
    utc_now,
)
from app.routes import date_separator_oob


def _sep_msg(created_at, reply_type=None):
    """An unsaved Message with just the fields the divider check cares about."""
    m = Message(content="x", reply_type=reply_type)
    m.created_at = created_at
    return m


class TestDateSeparatorOob:
    BASE = datetime.datetime(2026, 7, 16, 12, 0, 0)

    def test_same_day_emits_nothing(self, app):
        with app.app_context():
            prev = _sep_msg(self.BASE)
            msg = _sep_msg(self.BASE + datetime.timedelta(hours=6))
            assert date_separator_oob(msg, prev) == ""

    def test_new_day_emits_separator(self, app):
        with app.app_context():
            # 30/31 days back so the label is a real date, not Today/Yesterday.
            older = utc_now() - datetime.timedelta(days=31)
            newer = utc_now() - datetime.timedelta(days=30)
            html = date_separator_oob(_sep_msg(newer), _sep_msg(older))
            assert "date-separator" in html
            assert 'hx-swap-oob="beforeend:#message-list"' in html
            assert newer.strftime("%B") in html
            assert str(newer.day) in html

    def test_midnight_boundary_emits_separator(self, app):
        with app.app_context():
            prev = _sep_msg(datetime.datetime(2026, 7, 16, 23, 59, 30))
            msg = _sep_msg(datetime.datetime(2026, 7, 17, 0, 0, 10))
            assert "date-separator" in date_separator_oob(msg, prev)

    def test_no_previous_message_emits_separator(self, app):
        with app.app_context():
            assert "date-separator" in date_separator_oob(_sep_msg(self.BASE), None)

    def test_thread_reply_emits_nothing(self, app):
        with app.app_context():
            msg = _sep_msg(self.BASE, reply_type="thread")
            # Not rendered inline, so it must not drag a divider into the list.
            assert date_separator_oob(msg, None) == ""

    def test_today_label_for_a_message_sent_today(self, app):
        with app.app_context():
            prev = _sep_msg(utc_now() - datetime.timedelta(days=9))
            assert "Today" in date_separator_oob(_sep_msg(utc_now()), prev)


@pytest.fixture
def stale_channel(app):
    """A channel whose most recent message is 9 days old."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="date-sep-chan")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        ChannelMember.create(user=User.get_by_id(1), channel=channel)
        author = User.create(username="date-sep-author", email="ds@example.com")
        WorkspaceMember.create(user=author, workspace=workspace)
        old = Message.create(
            user=author,
            conversation=conv,
            content="last week",
            created_at=utc_now() - datetime.timedelta(days=9),
        )
        return conv.conversation_id_str, old.id


def _broadcast_html(broadcast_mock):
    """The raw HTML from the single chat_manager.broadcast call."""
    payload = broadcast_mock.call_args[0][1]
    return payload["_raw_html"] if isinstance(payload, dict) else payload


def test_first_message_of_a_new_day_broadcasts_a_separator(
    logged_in_client, app, stale_channel, mocker
):
    conv_id, _ = stale_channel
    broadcast = mocker.patch("app.routes.chat_manager.broadcast")
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

    res = logged_in_client.post(
        f"/chat/conversations/{conv_id}/messages",
        data={"chat_message": "morning"},
    )

    assert res.status_code == 204
    html = _broadcast_html(broadcast)
    assert "date-separator" in html
    assert "Today" in html
    # Divider first, then the message it heads.
    assert html.index("date-separator") < html.index("morning")


def test_second_message_same_day_broadcasts_no_separator(
    logged_in_client, app, stale_channel, mocker
):
    conv_id, _ = stale_channel
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")
    broadcast = mocker.patch("app.routes.chat_manager.broadcast")

    logged_in_client.post(
        f"/chat/conversations/{conv_id}/messages", data={"chat_message": "first"}
    )
    assert "date-separator" in _broadcast_html(broadcast)

    broadcast.reset_mock()
    logged_in_client.post(
        f"/chat/conversations/{conv_id}/messages", data={"chat_message": "second"}
    )
    assert "date-separator" not in _broadcast_html(broadcast)


def test_catchup_backfill_includes_separators_for_each_new_day(
    logged_in_client, app, stale_channel
):
    conv_id, old_id = stale_channel
    with app.app_context():
        conv = Conversation.get(conversation_id_str=conv_id)
        author = User.get(User.username == "date-sep-author")
        Message.create(
            user=author,
            conversation=conv,
            content="yesterday-msg",
            created_at=utc_now() - datetime.timedelta(days=1),
        )
        Message.create(user=author, conversation=conv, content="today-msg")

    res = logged_in_client.get(f"/chat/conversations/{conv_id}/messages/since/{old_id}")

    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert body.count("date-separator") == 2
    assert "Yesterday" in body
    assert "Today" in body
    assert body.index("Yesterday") < body.index("yesterday-msg")
