"""
Tests for the access checks inside ``_process_ws_event``.

The WebSocket transport itself is hard to exercise end-to-end (flask-sock's
upgrade machinery isn't reachable from the standard test client), so these
tests call ``_process_ws_event`` directly with a mock socket. That's the
function that handles ``send_message``, ``subscribe``, and typing events for
both the web (`/ws/chat`) and mobile (`/ws/api/v1`) routes.

The audit specifically called out the message-send path as missing a
membership check; the regression is that any authenticated user could post
into any conversation by sending a crafted frame with another conv's id.
"""

from unittest.mock import Mock

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
from app.routes import _process_ws_event


@pytest.fixture
def channel_and_member(app):
    """A channel the test user IS a member of."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="ws-auth-allowed")
        Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        ChannelMember.create(user=User.get_by_id(1), channel=channel)
        return channel.id


@pytest.fixture
def channel_no_member(app):
    """A channel the test user is NOT a member of."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="ws-auth-forbidden")
        Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        return channel.id


@pytest.fixture
def dm_with_partner(app):
    """A DM conversation between user 1 and a freshly created user 2."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        partner = User.create(
            username="ws-partner",
            email="ws-partner@example.com",
            display_name="WS Partner",
        )
        WorkspaceMember.create(user=partner, workspace=workspace)
        conv_id = f"dm_1_{partner.id}"
        Conversation.get_or_create(conversation_id_str=conv_id, defaults={"type": "dm"})
        return partner.id, conv_id


def _ws_for(user_id: int):
    """Build a Mock socket that resembles a connected user."""
    ws = Mock()
    ws.user = User.get_by_id(user_id)
    ws.user.username  # force-resolve so attribute access works under JSON.dumps
    ws.is_api_client = False
    return ws


# --- Allow path ---


class TestAllowedSends:
    def test_member_can_send_to_channel(self, app, channel_and_member, mocker):
        with app.app_context():
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )
            mocker.patch("app.routes._broadcast_regular_message")
            mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": f"channel_{channel_and_member}",
                    "chat_message": "hello",
                },
            )

            handle_new_message.assert_called_once()

    def test_dm_participant_can_send(self, app, dm_with_partner, mocker):
        with app.app_context():
            _, conv_id = dm_with_partner
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )
            mocker.patch("app.routes._broadcast_regular_message")
            mocker.patch("app.routes.chat_service.send_notifications_for_new_message")

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": conv_id,
                    "chat_message": "hello",
                },
            )

            handle_new_message.assert_called_once()


# --- Deny path (the security regression we're guarding against) ---


class TestDeniedSends:
    def test_non_member_send_to_channel_is_dropped(
        self, app, channel_no_member, mocker
    ):
        with app.app_context():
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": f"channel_{channel_no_member}",
                    "chat_message": "spam",
                },
            )

            handle_new_message.assert_not_called()
            assert Message.select().where(Message.content == "spam").count() == 0

    def test_outsider_send_to_dm_is_dropped(self, app, dm_with_partner, mocker):
        with app.app_context():
            partner_id, conv_id = dm_with_partner

            # Create a third user who is not part of the DM.
            workspace = Workspace.get(Workspace.name == "DevOcho")
            outsider = User.create(
                username="ws-outsider",
                email="ws-outsider@example.com",
                display_name="Outsider",
            )
            WorkspaceMember.create(user=outsider, workspace=workspace)

            ws = _ws_for(outsider.id)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": conv_id,
                    "chat_message": "lurking",
                },
            )

            handle_new_message.assert_not_called()

    def test_unknown_conversation_is_dropped(self, app, mocker):
        with app.app_context():
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": "channel_99999",
                    "chat_message": "ghost",
                },
            )

            handle_new_message.assert_not_called()

    def test_malformed_conv_id_is_dropped(self, app, channel_and_member, mocker):
        # Malformed conv id → DB lookup returns None first, so we hit the
        # "no conversation" path, not the parse path. Still must not crash
        # and must not write a message.
        with app.app_context():
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": "garbage",
                    "chat_message": "x",
                },
            )

            handle_new_message.assert_not_called()


# --- Empty/no-op events ---


class TestNoopEvents:
    def test_subscribe_routes_to_chat_manager(self, app, mocker):
        with app.app_context():
            ws = _ws_for(1)
            sub = mocker.patch("app.routes.chat_manager.subscribe")

            _process_ws_event(ws, {"type": "subscribe", "conversation_id": "channel_1"})

            sub.assert_called_once_with("channel_1", ws)

    def test_empty_message_drops_silently(self, app, channel_and_member, mocker):
        with app.app_context():
            ws = _ws_for(1)
            handle_new_message = mocker.patch(
                "app.routes.chat_service.handle_new_message"
            )

            _process_ws_event(
                ws,
                {
                    "type": "send_message",
                    "conversation_id": f"channel_{channel_and_member}",
                    "chat_message": "",
                },
            )

            handle_new_message.assert_not_called()
