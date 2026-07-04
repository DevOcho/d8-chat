"""Tests for the reconnect catch-up endpoints.

After a WebSocket reconnect the web client fetches messages newer than the last
one it has, plus a sidebar-unreads refresh, to recover anything broadcast while
it was disconnected (pub/sub is at-most-once).
"""

import pytest

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    User,
    Workspace,
    WorkspaceMember,
)


@pytest.fixture
def channel_with_messages(app):
    """A channel the default user is in, seeded with 3 messages."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="catchup-chan")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        ChannelMember.create(user=User.get_by_id(1), channel=channel)
        author = User.create(username="catchup-author", email="ca@example.com")
        WorkspaceMember.create(user=author, workspace=workspace)
        msgs = [
            Message.create(user=author, conversation=conv, content=f"m{i}")
            for i in range(3)
        ]
        return conv.conversation_id_str, [m.id for m in msgs]


def test_since_returns_only_newer_messages(logged_in_client, channel_with_messages):
    conv_id, ids = channel_with_messages
    res = logged_in_client.get(f"/chat/conversations/{conv_id}/messages/since/{ids[0]}")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # m0 (== ids[0]) excluded; m1 and m2 present.
    assert "message-{}".format(ids[1]) in body
    assert "message-{}".format(ids[2]) in body
    assert "message-{}".format(ids[0]) not in body


def test_since_zero_returns_all(logged_in_client, channel_with_messages):
    conv_id, ids = channel_with_messages
    res = logged_in_client.get(f"/chat/conversations/{conv_id}/messages/since/0")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    for mid in ids:
        assert f"message-{mid}" in body
    # OOB append wrapper present
    assert "hx-swap-oob" in body


def test_since_denies_non_member(logged_in_client, app):
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="catchup-forbidden")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        conv_id = conv.conversation_id_str
    res = logged_in_client.get(f"/chat/conversations/{conv_id}/messages/since/0")
    assert res.status_code == 403


def test_since_unknown_conversation_404(logged_in_client):
    res = logged_in_client.get("/chat/conversations/channel_99999/messages/since/0")
    assert res.status_code == 404


def test_since_truncation_header(logged_in_client, app, mocker):
    # Force a tiny cap so we can assert the truncated header cheaply.
    mocker.patch("app.routes.CATCHUP_LIMIT", 2)
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="catchup-trunc")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        ChannelMember.create(user=User.get_by_id(1), channel=channel)
        author = User.create(username="trunc-author", email="ta@example.com")
        WorkspaceMember.create(user=author, workspace=workspace)
        for i in range(5):
            Message.create(user=author, conversation=conv, content=f"t{i}")
        conv_id = conv.conversation_id_str

    res = logged_in_client.get(f"/chat/conversations/{conv_id}/messages/since/0")
    assert res.status_code == 200
    assert res.headers.get("X-D8-Catchup") == "truncated"


def test_sidebar_unreads_returns_badges(logged_in_client, channel_with_messages):
    conv_id, _ = channel_with_messages
    res = logged_in_client.get("/chat/sidebar/unreads")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # The seeded messages are from another user and unread, so a bold link or
    # badge for this conversation should be emitted.
    assert f"link-{conv_id}" in body
