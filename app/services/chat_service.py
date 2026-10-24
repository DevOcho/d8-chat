# app/services/chat_service.py

import datetime
import re

from flask import current_app, render_template, url_for
from peewee import fn

from app.chat_manager import chat_manager
from app.conversation_id import parse_conversation_id
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Hashtag,
    Mention,
    Message,
    MessageAttachment,
    MessageHashtag,
    User,
    UserConversationStatus,
    db,
    utc_now,
)
from app.services import minio_service, push_service

# How recent a user's activity stamp must be for a mobile push to be
# suppressed. Deliberately tighter than chat_manager.ACTIVE_TTL (the 15-minute
# presence-dot window): within ~5 minutes of walking away from the web app,
# new-message pushes start reaching the phone again.
PUSH_ACTIVE_MAX_AGE = 300

REPLY_TYPES = ("thread", "quote")

_MD_FENCE = re.compile(r"```[^\n]*\n?|~~~[^\n]*\n?")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_MARKS = re.compile(r"(\*\*|__|~~|`|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w))")
_MD_LINE_PREFIX = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)", re.MULTILINE
)


def plain_text(content, limit=None):
    """A message as plain text for places that can't render Markdown: desktop
    and mobile notifications and the conversation-list preview. Drops fences,
    emphasis marks, list/quote/heading prefixes and link targets, collapses
    whitespace, expands :emoji: shortcodes, and truncates to ``limit``."""
    # pylint: disable=import-outside-toplevel
    import emoji

    text = content or ""
    text = _MD_FENCE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_LINE_PREFIX.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = emoji.emojize(" ".join(text.split()), language="alias")
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


class MessageRefError(ValueError):
    """A client-supplied reference on a new message is invalid or forbidden."""


def parse_file_ids(attachment_file_ids):
    """Normalize attachment ids — a comma-separated string (web form) or a
    list (mobile JSON) — into a list of ints, dropping anything non-numeric."""
    if not attachment_file_ids:
        return []
    if isinstance(attachment_file_ids, (list, tuple)):
        parts = attachment_file_ids
    else:
        parts = str(attachment_file_ids).split(",")
    ids = []
    for part in parts:
        if isinstance(part, bool):
            continue
        text = str(part).strip()
        if text.isdigit():
            ids.append(int(text))
    return list(dict.fromkeys(ids))


def validate_message_refs(
    sender,
    conversation,
    parent_id=None,
    reply_type=None,
    quoted_message_id=None,
    attachment_file_ids=None,
):
    """Check the ids a client attached to a new message before it is created.

    Every one of them used to be trusted, so any member of one conversation
    could pull another conversation's content into it: quote (or quote-reply
    to) a private DM message by id, or attach someone else's upload, after
    which the file proxy authorized them to download it — and deleting their
    own message deleted the victim's file.

    Returns the cleaned ``(parent_id, reply_type, quoted_message_id,
    file_ids)``. Raises ``MessageRefError`` for a reference the sender can't
    use.
    """
    # pylint: disable=import-outside-toplevel
    from app.access import user_can_access_message
    from app.models import UploadedFile

    if reply_type is not None and reply_type not in REPLY_TYPES:
        raise MessageRefError("Unknown reply type.")

    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError) as exc:
            raise MessageRefError("Invalid parent message.") from exc
        parent = Message.get_or_none(id=parent_id)
        if parent is None or parent.conversation_id != conversation.id:
            raise MessageRefError("The message you replied to isn't here.")
        # Threads are one level deep: a reply to a thread reply joins the
        # thread it belongs to, or it would be invisible in the thread panel.
        if (
            reply_type == "thread"
            and parent.reply_type == "thread"
            and parent.parent_message_id
        ):
            parent_id = parent.parent_message_id
    else:
        reply_type = None

    if quoted_message_id is not None:
        try:
            quoted_message_id = int(quoted_message_id)
        except (TypeError, ValueError) as exc:
            raise MessageRefError("Invalid quoted message.") from exc
        quoted = Message.get_or_none(id=quoted_message_id)
        if not user_can_access_message(sender, quoted):
            raise MessageRefError("You can't quote that message.")

    file_ids = parse_file_ids(attachment_file_ids)
    if file_ids:
        owned = {
            f.id
            for f in UploadedFile.select(UploadedFile.id).where(
                (UploadedFile.id.in_(file_ids)) & (UploadedFile.uploader == sender)
            )
        }
        if owned != set(file_ids):
            raise MessageRefError("You can only attach files you uploaded.")

    return parent_id, reply_type, quoted_message_id, file_ids


def end_channel_membership(user_id, channel):
    """Remove ``user_id`` from ``channel`` and everything that followed from it.

    Deletes the membership and the user's read-state row — that row kept the
    channel's new messages flowing into an ex-member's Unreads — and stops
    their open sockets following the channel on every worker.
    """
    conv_id_str = f"channel_{channel.id}"
    ChannelMember.delete().where(
        (ChannelMember.user == user_id) & (ChannelMember.channel == channel)
    ).execute()
    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
    if conversation is not None:
        UserConversationStatus.delete().where(
            (UserConversationStatus.user == user_id)
            & (UserConversationStatus.conversation == conversation)
        ).execute()
    try:
        chat_manager.unsubscribe_user_everywhere(user_id, conv_id_str)
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception(
            "Couldn't unsubscribe user %s from %s", user_id, conv_id_str
        )


def delete_message_and_files(message):
    """Delete ``message`` and the uploads that belong to it alone.

    A message that starts a thread isn't removed: deleting the row orphaned
    its replies (parent set to NULL), so they disappeared from both the thread
    panel and the timeline. It becomes a placeholder instead — content,
    attachments, reactions, mentions, hashtags and poll removed,
    ``is_deleted`` set. Returns True for a placeholder, False for a delete.

    A file is removed only when the message's author uploaded it and nothing
    else uses it — no other message (a forward reuses the original's file ids)
    and no avatar. Deleting every attached file let a user destroy someone
    else's upload by attaching it to their own message and deleting that, and
    broke forwards of the deleted message. Object storage is touched only after
    the DB transaction commits, so a rollback can't leave rows pointing at a
    deleted object.
    """
    # pylint: disable=import-outside-toplevel
    from app.models import UploadedFile

    candidates = [f for f in message.attachments if f.uploader_id == message.user_id]
    orphans = []
    for uploaded in candidates:
        shared = (
            MessageAttachment.select()
            .where(
                (MessageAttachment.attachment == uploaded)
                & (MessageAttachment.message != message)
            )
            .exists()
        )
        is_avatar = User.select().where(User.avatar == uploaded).exists()
        if not shared and not is_avatar:
            orphans.append(uploaded)

    # pylint: disable=import-outside-toplevel
    from app.models import Poll, Reaction

    has_thread = (
        Message.select()
        .where((Message.parent_message == message) & (Message.reply_type == "thread"))
        .exists()
    )
    with db.atomic():
        if has_thread:
            for model in (Reaction, Mention, MessageHashtag, MessageAttachment):
                model.delete().where(model.message == message).execute()
            poll = Poll.get_or_none(message=message)
            if poll is not None:
                poll.delete_instance(recursive=True)
            Message.update(content="", is_deleted=True, is_edited=False).where(
                Message.id == message.id
            ).execute()
        else:
            message.delete_instance(recursive=True)
        if orphans:
            UploadedFile.delete().where(
                UploadedFile.id.in_([f.id for f in orphans])
            ).execute()

    for uploaded in orphans:
        try:
            minio_service.delete_file(uploaded.stored_filename)
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception(
                "Couldn't delete object %s for message %s",
                uploaded.stored_filename,
                message.id,
            )
    return has_thread


def handle_new_message(
    sender: User,
    conversation: Conversation,
    chat_text: str,
    parent_id: int = None,
    reply_type: str = None,
    attachment_file_ids: str = None,
    quoted_message_id: int = None,
):
    """
    Handles the business logic for creating a new message and its associated mentions and hashtags.

    Args:
        sender: The User object of the message sender.
        conversation: The Conversation object where the message was sent.
        chat_text: The raw content of the message.
        parent_id: The ID of the parent message, if it's a reply.
        attachment_file_ids: A comma-separated string of UploadedFile IDs.
        quoted_message_id: The ID of a message being quoted.

    Returns:
        The newly created Message object.
    """

    with db.atomic():
        # Step 1: Create the core message object
        new_message = Message.create(
            user=sender,
            conversation=conversation,
            content=chat_text,
            parent_message=parent_id if parent_id else None,
            reply_type=reply_type if parent_id else None,
            quoted_message=quoted_message_id if quoted_message_id else None,
        )

        # If this is a thread reply, update the parent's last_reply_at timestamp
        # (A targeted UPDATE, not a full-row save that could revert a
        # concurrent edit of the parent.)
        if reply_type == "thread" and parent_id:
            Message.update(last_reply_at=new_message.created_at).where(
                Message.id == parent_id
            ).execute()

        # Step 2: Link any attachments if IDs are provided
        for file_id in parse_file_ids(attachment_file_ids):
            MessageAttachment.create(message=new_message, attachment=file_id)

        # --- Mention handling logic ---
        # 1. Handle regular @username mentions
        mention_pattern = r"(?<![^\s(\['\"])@(\w+)"
        mentioned_usernames = set(re.findall(mention_pattern, chat_text))
        mentioned_usernames.discard("here")
        mentioned_usernames.discard("channel")

        if mentioned_usernames:
            mentioned_users = User.select().where(
                User.username.in_(list(mentioned_usernames))
            )
            for mentioned_user in mentioned_users:
                # check to prevent users from creating a mention for themselves
                if mentioned_user.id != sender.id:
                    Mention.get_or_create(user=mentioned_user, message=new_message)

        # 2. Handle @channel and @here mentions (only applies to channels)
        if conversation.type == "channel":
            channel = Channel.get_by_id(
                parse_conversation_id(conversation.conversation_id_str).channel_id
            )
            target_ids = set()

            # Handle @channel - all members
            if "@channel" in chat_text:
                target_ids.update(
                    m.user_id
                    for m in ChannelMember.select(ChannelMember.user).where(
                        ChannelMember.channel == channel
                    )
                )

            # Handle @here - only online members
            if "@here" in chat_text:
                member_ids = {
                    m.user_id
                    for m in ChannelMember.select(ChannelMember.user).where(
                        ChannelMember.channel == channel
                    )
                }
                target_ids.update(member_ids & chat_manager.online_user_ids())

            # 3. Create Mention records for the collected users in one
            # statement: a get_or_create per member ran two queries each inside
            # the send transaction, ~800 for a 200-member channel before the
            # message was even broadcast. Direct @mentions made above are
            # skipped by the conflict clause.
            target_ids.discard(sender.id)
            if target_ids:
                now = utc_now()
                Mention.insert_many(
                    [
                        {
                            "user": uid,
                            "message": new_message.id,
                            "created_at": now,
                            "updated_at": now,
                        }
                        for uid in target_ids
                    ]
                ).on_conflict_ignore().execute()

        # --- Hashtag handling logic ---
        # 1. Find all potential hashtags in the message content.
        hashtag_pattern = r"(?<![^\s(\['\"])#([a-zA-Z0-9_-]+)"
        hashtag_names = set(re.findall(hashtag_pattern, chat_text))

        # 2. Loop through the found tags, get or create them, and link to the message.
        if hashtag_names:
            # Find which of these are actual channels to exclude them
            existing_channels = {
                c.name
                for c in Channel.select().where(Channel.name.in_(list(hashtag_names)))
            }

            valid_hashtags = hashtag_names - existing_channels

            for tag_name in valid_hashtags:
                hashtag, _ = Hashtag.get_or_create(name=tag_name)
                MessageHashtag.create(message=new_message, hashtag=hashtag)

    return new_message


def send_notifications_for_new_message(new_message: Message, sender_user: User):
    """
    Analyzes a new message and sends real-time UI updates (badges, sounds, etc.)
    to all relevant users who are NOT actively viewing the conversation.

    The per-member work is batched: read-state rows, mentions and the channel
    are loaded once, the common "this new message is unread" case needs no
    query, and identical fragments are rendered once. It used to run ~7
    queries, 2-3 renders and 2-4 publishes per online member, holding a pool
    connection the whole time.
    """
    conversation = new_message.conversation
    conv_id_str = conversation.conversation_id_str
    parsed_conv = parse_conversation_id(conv_id_str)
    # A status row created now — the recipient never opened the conversation,
    # or "left" the DM, which deletes theirs — must start out with this
    # message unread. The default (now) is later than the message, so it read
    # as already seen: no badge, no sidebar row, only the sound, and the DM
    # surfaced on the next page load.
    unread_defaults = {
        "last_read_timestamp": new_message.created_at
        - datetime.timedelta(microseconds=1)
    }

    # Determine who the members of the conversation are
    channel = None
    if conversation.type == "channel":
        channel = Channel.get_by_id(parsed_conv.channel_id)
        members = list(
            User.select().join(ChannelMember).where(ChannelMember.channel == channel)
        )
    else:  # DM
        members = list(User.select().where(User.id.in_(list(parsed_conv.user_ids))))
        # Guarantee both participants a status row even when they're offline.
        # The web sidebar and GET /api/v1/dms join through this table, so a
        # missing row hides the conversation entirely — the recipient never
        # saw the DM until they searched for the sender and started one
        # themselves. (The loop below get_or_creates too, but only after
        # skipping offline members.)
        for member in members:
            UserConversationStatus.get_or_create(
                user=member,
                conversation=conversation,
                defaults=unread_defaults if member.id != sender_user.id else {},
            )

    # Condition 1: Don't notify the sender or any offline users (cluster-aware).
    # Condition 2 (viewing the conversation) is the 'exclude_channel' on each
    # send_to_user.
    recipients = [
        m
        for m in members
        if m.id != sender_user.id and chat_manager.is_user_online_in_cluster(m.id)
    ]
    if recipients:
        _notify_online_recipients(
            new_message, sender_user, conversation, channel, recipients, unread_defaults
        )

    _dispatch_push_notifications(new_message, sender_user, conversation, parsed_conv)


def _notify_online_recipients(
    new_message, sender_user, conversation, channel, recipients, unread_defaults
):
    """Badges, sounds and desktop notifications for the online recipients."""
    conv_id_str = conversation.conversation_id_str
    statuses = {
        s.user_id: s
        for s in UserConversationStatus.select().where(
            (UserConversationStatus.conversation == conversation)
            & (UserConversationStatus.user.in_([m.id for m in recipients]))
        )
    }
    mentioned_ids = {
        m.user_id
        for m in Mention.select(Mention.user).where(Mention.message == new_message)
    }
    in_timeline = new_message.reply_type != "thread"
    unread_link_html = render_template("partials/unreads_link_unread.html")
    bold_link_html = None
    icon = url_for("static", filename="favicon.ico")
    sender_name = new_message.user.display_name or new_message.user.username
    now = utc_now()

    missing = [m.id for m in recipients if m.id not in statuses]
    if missing:
        # One INSERT for members who never opened the conversation, instead of
        # a get_or_create (two queries plus a transaction) each.
        stamp = utc_now()
        UserConversationStatus.insert_many(
            [
                {
                    "user": uid,
                    "conversation": conversation,
                    "created_at": stamp,
                    "updated_at": stamp,
                    **unread_defaults,
                }
                for uid in missing
            ]
        ).on_conflict_ignore().execute()
        statuses.update(
            {
                s.user_id: s
                for s in UserConversationStatus.select().where(
                    (UserConversationStatus.conversation == conversation)
                    & (UserConversationStatus.user.in_(missing))
                )
            }
        )

    for member in recipients:
        status = statuses.get(member.id)
        if status is None:
            continue
        is_mention = member.id in mentioned_ids
        notification_html = None
        unread_count = 1

        if channel is not None:
            link_text = f"# {channel.name}"
            hx_get_url = url_for("channels.get_channel_chat", channel_id=channel.id)
            if is_mention:
                unread_count = (
                    Mention.select()
                    .join(Message)
                    .where(
                        (Message.created_at > status.last_read_timestamp)
                        & (Mention.user == member)
                        & (Message.conversation == conversation)
                    )
                    .count()
                )
                notification_html = render_template(
                    "partials/unread_badge.html",
                    conv_id_str=conv_id_str,
                    count=unread_count,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )
            elif _has_unread_after(
                new_message, conversation, member, status, in_timeline
            ):
                if bold_link_html is None:
                    bold_link_html = render_template(
                        "partials/bold_link.html",
                        conv_id_str=conv_id_str,
                        link_text=link_text,
                        hx_get_url=hx_get_url,
                    )
                notification_html = bold_link_html
        else:  # DM
            unread_count = (
                Message.select()
                .where(
                    (Message.conversation == conversation)
                    & (Message.created_at > status.last_read_timestamp)
                    & (Message.user != member)
                    & Message.in_timeline()
                )
                .count()
            )
            if unread_count > 0:
                # A sidebar row rides along with the badge: if this DM isn't in
                # the client's sidebar yet (brand-new conversation, or the
                # dm_created frame was lost while their socket was down), the
                # badge's OOB target doesn't exist and the whole notification
                # would be silently dropped. The client moves the existing row
                # instead when it has one (htmx:oobBeforeSwap in chat.js).
                sidebar_row_html = render_template(
                    "partials/dm_list_item_oob.html",
                    user=sender_user,
                    conv_id_str=conv_id_str,
                    is_online=chat_manager.is_user_online_in_cluster(sender_user.id),
                    is_active=chat_manager.is_user_active(sender_user.id),
                )
                notification_html = sidebar_row_html + render_template(
                    "partials/unread_badge.html",
                    conv_id_str=conv_id_str,
                    count=unread_count,
                    link_text=sender_user.display_name or sender_user.username,
                    hx_get_url=url_for("dms.get_dm_chat", other_user_id=sender_user.id),
                )

        if notification_html:
            # One frame: the badge plus the Unreads link for web clients, and
            # the structured payload for the mobile app.
            api_data = {
                "type": "unread_updated",
                "data": {
                    "conversation_id_str": conv_id_str,
                    "unread_count": unread_count,
                    "is_mention": bool(channel is not None and is_mention),
                },
            }
            chat_manager.send_to_user(
                member.id,
                {
                    "_raw_html": notification_html + unread_link_html,
                    "api_data": api_data,
                },
                exclude_channel=conv_id_str,
            )

        # Sound and desktop notification
        if is_mention:
            title = f"New mention from {sender_name}"
        elif channel is None and (
            status.last_notified_timestamp is None
            or (now - status.last_notified_timestamp) > datetime.timedelta(seconds=10)
        ):
            title = f"New message from {sender_name}"
        else:
            continue
        chat_manager.send_to_user(
            member.id, {"type": "sound"}, exclude_channel=conv_id_str
        )
        chat_manager.send_to_user(
            member.id,
            {
                "type": "notification",
                "title": title,
                "body": plain_text(new_message.content, limit=240),
                # A relative path, not _external=True, so it's always same-origin.
                "icon": icon,
                "tag": conv_id_str,
            },
            exclude_channel=conv_id_str,
        )
        _mark_notified(member, conversation, now)


def _has_unread_after(new_message, conversation, member, status, in_timeline):
    """Whether ``member`` has an unread timeline message here from someone else.

    The new message itself answers it in the common case (it's in the
    timeline and newer than their last read), with no query.
    """
    if in_timeline and new_message.created_at > status.last_read_timestamp:
        return True
    return (
        Message.select()
        .where(
            (Message.conversation == conversation)
            & (Message.created_at > status.last_read_timestamp)
            & (Message.user != member)
            & Message.in_timeline()
        )
        .exists()
    )


def _mark_notified(user, conversation, now):
    """Stamp last_notified_timestamp with a targeted UPDATE. The fan-out loads
    the status row, then spends milliseconds on queries, renders and publishes;
    saving the whole row afterwards wrote back a stale last_read_timestamp, so
    a DM the user read in the meantime showed as unread again."""
    UserConversationStatus.update(last_notified_timestamp=now).where(
        (UserConversationStatus.user == user)
        & (UserConversationStatus.conversation == conversation)
    ).execute()


def _push_recipients(new_message, sender_user, conversation, parsed_conv):
    """Return the set of user ids that should get a mobile push for this message.

    Triggers (per product decision):
      - DM     → the other DM participant
      - @mention of a user → that user
      - Thread reply → every prior thread participant (everyone who ever
        replied in this thread, plus the thread starter)
      - NOT @channel / @here — too noisy for v1; revisit if requested.

    The sender is always excluded. Online filtering happens at the caller
    so this function stays easy to test in isolation.
    """
    recipient_ids = set()

    if conversation.type == "dm":
        for uid in parsed_conv.user_ids:
            recipient_ids.add(uid)

    # Mention rows exist for both direct @username mentions *and* the
    # @channel / @here fan-out (the latter creates a row per member). Only
    # the direct mentions should drive push — bulk @channel pings would be
    # too noisy on mobile. Filter by checking the message body for each
    # mentioned user's literal @username.
    content = new_message.content or ""
    direct_mention_pattern = r"(?<![^\s(\['\"])@(\w+)"
    direct_usernames = {m.lower() for m in re.findall(direct_mention_pattern, content)}
    direct_usernames -= {"channel", "here"}

    if direct_usernames:
        mentioned_ids = {
            u.id
            for u in User.select(User.id, User.username).where(
                fn.LOWER(User.username).in_(list(direct_usernames))
            )
        }
        recipient_ids.update(mentioned_ids)

    if new_message.reply_type == "thread" and new_message.parent_message_id:
        thread_user_ids = {
            row.user_id
            for row in Message.select(Message.user).where(
                (Message.parent_message == new_message.parent_message_id)
                & (Message.reply_type == "thread")
            )
        }
        parent = Message.get_or_none(id=new_message.parent_message_id)
        if parent is not None and parent.user_id is not None:
            thread_user_ids.add(parent.user_id)
        recipient_ids.update(thread_user_ids)

    recipient_ids.discard(sender_user.id)
    return recipient_ids


def _dispatch_push_notifications(new_message, sender_user, conversation, parsed_conv):
    """Send a mobile push to every offline recipient for this message.

    No-op when ``push_service`` isn't configured (self-hosters without a
    Firebase project). Wraps each per-user dispatch in a try/except so a
    single bad token can't poison the rest of the recipient loop.
    """
    if not push_service.is_configured():
        return

    recipients = _push_recipients(new_message, sender_user, conversation, parsed_conv)
    if not recipients:
        return

    sender_label = sender_user.display_name or sender_user.username
    if conversation.type == "dm":
        title = f"New message from {sender_label}"
    elif new_message.reply_type == "thread":
        title = f"{sender_label} replied in a thread"
    else:
        title = f"{sender_label} mentioned you"

    body = plain_text(new_message.content, limit=240)

    payload_data = {
        "conversation_id_str": conversation.conversation_id_str,
        "message_id": new_message.id,
    }
    if new_message.parent_message_id:
        payload_data["parent_message_id"] = new_message.parent_message_id

    for user_id in recipients:
        # Suppress the mobile push only while the person is really using the
        # app somewhere: a live socket AND a recent activity stamp. A browser
        # tab left open on another desk used to count as "online" and swallow
        # the push forever. Web input pings re-stamp activity every ≤4 minutes
        # while someone is at the keyboard, and a foregrounded mobile app's
        # socket heartbeat re-stamps every 30s, so anyone genuinely present
        # stays inside PUSH_ACTIVE_MAX_AGE.
        if chat_manager.is_user_online_in_cluster(
            user_id
        ) and chat_manager.is_user_active(user_id, max_age=PUSH_ACTIVE_MAX_AGE):
            continue
        try:
            push_service.send_to_user(
                user_id, title=title, body=body, data=payload_data
            )
        except Exception:  # pylint: disable=broad-except
            # push_service already logs; we just don't want one bad
            # recipient to break the rest of the loop.
            continue
