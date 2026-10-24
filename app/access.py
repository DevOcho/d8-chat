"""
Conversation access checks shared across blueprints.

The same "is this user a member of this conversation?" query was inlined at
half a dozen sites (REST endpoints in ``api_v1.py`` plus the shared WebSocket
handler). When the WS handler was added, the inline check was forgotten —
which left a hole where any authenticated user could post a message into any
conversation by sending a crafted WS frame. Centralizing the check here means
new callers can't forget it and the policy lives in one place.
"""

from .conversation_id import ConversationKey, parse_conversation_id
from .models import Channel, ChannelMember


def user_has_conversation_access(user, parsed: ConversationKey) -> bool:
    """
    True if ``user`` is a member of the conversation described by ``parsed``.

    For channels we check ``ChannelMember`` directly. For DMs the participant
    list is encoded in the conversation id itself, so membership is just
    ``user.id in parsed.user_ids``. Unknown conversation types deny access by
    default — callers should never see one in practice (the parser rejects
    them) but defaulting to ``False`` keeps the policy fail-closed.
    """
    if user is None:
        return False
    if parsed.type == "channel":
        return (
            ChannelMember.select()
            .where(
                (ChannelMember.user == user)
                & (ChannelMember.channel_id == parsed.channel_id)
            )
            .exists()
        )
    if parsed.type == "dm":
        return user.id in parsed.user_ids
    return False


def user_can_access_conversation(user, conversation) -> bool:
    """``user_has_conversation_access`` for a ``Conversation`` row.

    A malformed stored id denies access rather than raising.
    """
    if conversation is None:
        return False
    try:
        parsed = parse_conversation_id(conversation.conversation_id_str)
    except ValueError:
        return False
    return user_has_conversation_access(user, parsed)


def user_can_access_message(user, message) -> bool:
    """True if ``user`` may see ``message``: they belong to its conversation.

    Every route that renders or acts on a message by id must check this, or a
    guessed id reads another conversation's content (a private channel or a
    DM the user isn't in).
    """
    if message is None:
        return False
    return user_can_access_conversation(user, message.conversation)


def user_can_post(user, parsed: ConversationKey) -> bool:
    """True if ``user`` may post into the conversation: a member, and an admin
    of the channel when posting there is restricted to admins.

    Shared by every send path (web, WebSocket, mobile API, polls, forwards) so
    none of them can skip the posting restriction.
    """
    if not user_has_conversation_access(user, parsed):
        return False
    if parsed.type == "channel":
        channel = Channel.get_or_none(id=parsed.channel_id)
        if channel is None:
            return False
        if channel.posting_restricted_to_admins:
            membership = ChannelMember.get_or_none(user=user, channel=channel)
            return membership is not None and membership.role == "admin"
    return True
