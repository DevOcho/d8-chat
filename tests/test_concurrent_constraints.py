"""
Constraint-level guarantees that protect against concurrent-write races.

We can't easily induce real thread races in pytest, but the actual
correctness guarantee for these features is at the DB level: composite
primary keys make duplicate rows impossible regardless of how the writes
interleave. The cheapest meaningful test is to confirm those constraints
exist and that the second insert really does get refused with
``IntegrityError``.

If a future schema change drops one of these constraints, these tests will
catch it.
"""

import pytest
from peewee import IntegrityError

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    Reaction,
    User,
    Workspace,
    WorkspaceMember,
)


@pytest.fixture
def message(app):
    """Create a real message we can react to / mention against."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="constraints")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}",
            defaults={"type": "channel"},
        )
        user = User.get_by_id(1)
        msg = Message.create(user=user, conversation=conv, content="hi")
        return msg.id


class TestReactionUniqueness:
    def test_same_user_emoji_message_combo_is_unique(self, app, message):
        with app.app_context():
            user = User.get_by_id(1)
            msg = Message.get_by_id(message)
            Reaction.create(user=user, message=msg, emoji="👍")
            with pytest.raises(IntegrityError):
                Reaction.create(user=user, message=msg, emoji="👍")

    def test_different_emoji_same_message_is_allowed(self, app, message):
        with app.app_context():
            user = User.get_by_id(1)
            msg = Message.get_by_id(message)
            Reaction.create(user=user, message=msg, emoji="👍")
            Reaction.create(user=user, message=msg, emoji="🎉")  # no error
            assert Reaction.select().where(Reaction.message == msg).count() == 2


class TestMentionUniqueness:
    def test_same_user_message_combo_is_unique(self, app, message):
        with app.app_context():
            user = User.get_by_id(1)
            msg = Message.get_by_id(message)
            Mention.create(user=user, message=msg)
            with pytest.raises(IntegrityError):
                Mention.create(user=user, message=msg)


class TestChannelMemberDoesntDoubleAdd:
    """``ChannelMember`` doesn't have a composite PK on (user, channel) — it
    uses a surrogate ``id`` — but the admin and channel-add code relies on
    ``get_or_create`` to make adds idempotent. Confirm that the calling
    code's idempotency holds, and document that direct ``create`` would
    insert a duplicate. If a future migration adds a unique constraint,
    update this test to ``pytest.raises(IntegrityError)``.
    """

    def test_get_or_create_is_idempotent(self, app):
        with app.app_context():
            workspace = Workspace.get(Workspace.name == "DevOcho")
            channel = Channel.create(workspace=workspace, name="member-idem")
            user = User.get_by_id(1)
            ChannelMember.get_or_create(user=user, channel=channel)
            ChannelMember.get_or_create(user=user, channel=channel)
            assert (
                ChannelMember.select()
                .where(
                    (ChannelMember.user == user) & (ChannelMember.channel == channel)
                )
                .count()
                == 1
            )


class TestWorkspaceMemberIdempotency:
    def test_get_or_create_is_idempotent(self, app):
        with app.app_context():
            workspace = Workspace.get(Workspace.name == "DevOcho")
            user = User.get_by_id(1)
            WorkspaceMember.get_or_create(user=user, workspace=workspace)
            WorkspaceMember.get_or_create(user=user, workspace=workspace)
            assert (
                WorkspaceMember.select()
                .where(
                    (WorkspaceMember.user == user)
                    & (WorkspaceMember.workspace == workspace)
                )
                .count()
                == 1
            )
