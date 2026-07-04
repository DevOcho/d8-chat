"""Tests for the web HTTP message-send endpoint.

Web clients POST messages to /chat/conversations/<conv_id_str>/messages instead
of sending over the WebSocket, so the sender gets a real success/failure. The
endpoint shares handle_inbound_message with the WS path, so it must enforce the
same access rules. These mirror the deny/allow cases in test_ws_event_auth.py
but drive the real HTTP route via the logged-in test client.
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
def channel_member_conv(app):
    """A channel conversation the default user (id=1) is a member of."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="http-send-allowed")
        Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        ChannelMember.create(user=User.get_by_id(1), channel=channel)
        return f"channel_{channel.id}"


@pytest.fixture
def channel_nonmember_conv(app):
    """A channel conversation the default user is NOT a member of."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="http-send-forbidden")
        Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        return f"channel_{channel.id}"


@pytest.fixture
def dm_conv(app):
    """A DM conversation between user 1 and a fresh partner."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        partner = User.create(
            username="http-partner",
            email="http-partner@example.com",
            display_name="HTTP Partner",
        )
        WorkspaceMember.create(user=partner, workspace=workspace)
        conv_id = f"dm_1_{partner.id}"
        Conversation.get_or_create(conversation_id_str=conv_id, defaults={"type": "dm"})
        return conv_id


def test_member_can_post_message(logged_in_client, app, channel_member_conv, mocker):
    # Don't require a live pub/sub or notification fan-out for this test.
    mocker.patch("app.routes._broadcast_regular_message")
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

    res = logged_in_client.post(
        f"/chat/conversations/{channel_member_conv}/messages",
        data={"chat_message": "hello over http"},
    )

    assert res.status_code == 204
    with app.app_context():
        assert Message.select().where(Message.content == "hello over http").count() == 1


def test_non_member_post_is_forbidden(
    logged_in_client, app, channel_nonmember_conv, mocker
):
    handle = mocker.patch("app.routes.chat_service.handle_new_message")

    res = logged_in_client.post(
        f"/chat/conversations/{channel_nonmember_conv}/messages",
        data={"chat_message": "spam"},
    )

    assert res.status_code == 403
    handle.assert_not_called()


def test_dm_participant_can_post(logged_in_client, app, dm_conv, mocker):
    mocker.patch("app.routes._broadcast_regular_message")
    mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

    res = logged_in_client.post(
        f"/chat/conversations/{dm_conv}/messages",
        data={"chat_message": "hi partner"},
    )

    assert res.status_code == 204


def test_dm_outsider_post_is_forbidden(logged_in_client, app, dm_conv, mocker):
    # dm_conv is between user 1 and the partner; user 1 IS a participant, so to
    # test the outsider path we craft a DM id that excludes user 1.
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        a = User.create(username="dm-a", email="dm-a@example.com")
        b = User.create(username="dm-b", email="dm-b@example.com")
        WorkspaceMember.create(user=a, workspace=workspace)
        WorkspaceMember.create(user=b, workspace=workspace)
        other_conv = f"dm_{a.id}_{b.id}"
        Conversation.get_or_create(
            conversation_id_str=other_conv, defaults={"type": "dm"}
        )

    handle = mocker.patch("app.routes.chat_service.handle_new_message")
    res = logged_in_client.post(
        f"/chat/conversations/{other_conv}/messages",
        data={"chat_message": "lurking"},
    )

    assert res.status_code == 403
    handle.assert_not_called()


def test_empty_message_is_rejected(logged_in_client, app, channel_member_conv):
    res = logged_in_client.post(
        f"/chat/conversations/{channel_member_conv}/messages",
        data={"chat_message": ""},
    )
    assert res.status_code == 400


def test_unknown_conversation_is_404(logged_in_client, app):
    res = logged_in_client.post(
        "/chat/conversations/channel_99999/messages",
        data={"chat_message": "ghost"},
    )
    assert res.status_code == 404


def test_restricted_channel_blocks_non_admin(
    logged_in_client, app, channel_member_conv, mocker
):
    with app.app_context():
        conv = Conversation.get(Conversation.conversation_id_str == channel_member_conv)
        channel_id = int(channel_member_conv.split("_", 1)[1])
        channel = Channel.get_by_id(channel_id)
        channel.posting_restricted_to_admins = True
        channel.save()
        # ensure the membership role is not admin
        member = ChannelMember.get(
            (ChannelMember.user == 1) & (ChannelMember.channel == channel)
        )
        member.role = "member"
        member.save()
        assert conv  # sanity

    handle = mocker.patch("app.routes.chat_service.handle_new_message")
    res = logged_in_client.post(
        f"/chat/conversations/{channel_member_conv}/messages",
        data={"chat_message": "not allowed"},
    )
    assert res.status_code == 403
    handle.assert_not_called()
