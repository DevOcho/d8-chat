"""
Parsing for conversation ID strings.

Conversations are identified throughout the app by string keys stored on
`Conversation.conversation_id_str`:

- ``channel_<id>`` — a workspace channel.
- ``dm_<u1>_<u2>[_<u3>...]`` — a direct message between two or more users.

Several blueprints accept these strings directly from URLs (e.g.
``/api/v1/conversations/<conv_id_str>/messages``). Inline ``split('_')`` /
``int(...)`` parsing was scattered across the codebase and would raise
unhandled ``ValueError`` on malformed input. Parse via this helper at every
trust boundary (anywhere the string came from a request) and let the caller
decide how to map a parse failure onto an HTTP response.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationKey:
    """Structured form of a parsed conversation ID."""

    type: str  # "channel" or "dm"
    channel_id: int | None = None
    user_ids: tuple[int, ...] = ()


def parse_conversation_id(conv_id_str: str) -> ConversationKey:
    """
    Parse a conversation ID string into a ``ConversationKey``.

    Raises ``ValueError`` for any malformed input — including empty strings,
    unknown type prefixes, missing IDs, or non-integer ID components. Callers
    at HTTP boundaries should catch this and return 400.
    """
    if not isinstance(conv_id_str, str) or not conv_id_str:
        raise ValueError("conversation id must be a non-empty string")

    parts = conv_id_str.split("_")
    if len(parts) < 2:
        raise ValueError(f"malformed conversation id: {conv_id_str!r}")

    kind, *id_parts = parts
    try:
        ids = [int(p) for p in id_parts]
    except ValueError as exc:
        raise ValueError(
            f"non-integer component in conversation id {conv_id_str!r}"
        ) from exc

    if kind == "channel":
        if len(ids) != 1:
            raise ValueError(
                f"channel conversation id must have exactly one id: {conv_id_str!r}"
            )
        return ConversationKey(type="channel", channel_id=ids[0])

    if kind == "dm":
        if len(ids) < 2:
            raise ValueError(
                f"dm conversation id must have at least two user ids: {conv_id_str!r}"
            )
        return ConversationKey(type="dm", user_ids=tuple(ids))

    raise ValueError(f"unknown conversation type {kind!r} in {conv_id_str!r}")
