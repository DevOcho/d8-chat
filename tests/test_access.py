"""
Tests for ``app.access.user_has_conversation_access``.

This is the gate that REST endpoints already used and that the WS handler
used to skip. Centralizing it closes the WS hole and gives us a single place
to verify the policy.
"""

from app.access import user_has_conversation_access
from app.conversation_id import parse_conversation_id
from app.models import Channel, ChannelMember, User, Workspace


def _make_extra_user(username: str) -> User:
    workspace = Workspace.get(Workspace.name == "DevOcho")
    user = User.create(
        username=username,
        email=f"{username}@example.com",
        display_name=username.title(),
    )
    from app.models import WorkspaceMember

    WorkspaceMember.create(user=user, workspace=workspace)
    return user


def _make_extra_channel(name: str) -> Channel:
    workspace = Workspace.get(Workspace.name == "DevOcho")
    return Channel.create(workspace=workspace, name=name)


class TestChannelAccess:
    def test_member_has_access(self, app):
        with app.app_context():
            user = User.get_by_id(1)
            channel = _make_extra_channel("project-alpha")
            ChannelMember.create(user=user, channel=channel)
            parsed = parse_conversation_id(f"channel_{channel.id}")
            assert user_has_conversation_access(user, parsed) is True

    def test_non_member_denied(self, app):
        with app.app_context():
            user = User.get_by_id(1)
            channel = _make_extra_channel("project-bravo")
            # No ChannelMember row created.
            parsed = parse_conversation_id(f"channel_{channel.id}")
            assert user_has_conversation_access(user, parsed) is False

    def test_membership_for_other_channel_doesnt_grant(self, app):
        with app.app_context():
            user = User.get_by_id(1)
            allowed = _make_extra_channel("allowed")
            other = _make_extra_channel("other")
            ChannelMember.create(user=user, channel=allowed)
            parsed = parse_conversation_id(f"channel_{other.id}")
            assert user_has_conversation_access(user, parsed) is False


class TestDmAccess:
    def test_participant_has_access(self, app):
        with app.app_context():
            user = User.get_by_id(1)
            other = _make_extra_user("alice_dm")
            parsed = parse_conversation_id(f"dm_{user.id}_{other.id}")
            assert user_has_conversation_access(user, parsed) is True

    def test_outsider_denied(self, app):
        with app.app_context():
            outsider = _make_extra_user("eve")
            a = _make_extra_user("a")
            b = _make_extra_user("b")
            parsed = parse_conversation_id(f"dm_{a.id}_{b.id}")
            assert user_has_conversation_access(outsider, parsed) is False

    def test_self_dm(self, app):
        with app.app_context():
            user = User.get_by_id(1)
            parsed = parse_conversation_id(f"dm_{user.id}_{user.id}")
            assert user_has_conversation_access(user, parsed) is True


class TestEdgeCases:
    def test_none_user_denied(self, app):
        with app.app_context():
            channel = _make_extra_channel("public")
            parsed = parse_conversation_id(f"channel_{channel.id}")
            assert user_has_conversation_access(None, parsed) is False
