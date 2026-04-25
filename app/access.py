"""
Conversation access checks shared across blueprints.

The same "is this user a member of this conversation?" query was inlined at
half a dozen sites (REST endpoints in ``api_v1.py`` plus the shared WebSocket
handler). When the WS handler was added, the inline check was forgotten —
which left a hole where any authenticated user could post a message into any
conversation by sending a crafted WS frame. Centralizing the check here means
new callers can't forget it and the policy lives in one place.
"""

from .conversation_id import ConversationKey
from .models import ChannelMember


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
