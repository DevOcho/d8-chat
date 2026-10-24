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


def delete_message_and_files(message):
    """Delete ``message`` and the uploads that belong to it alone.

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

    with db.atomic():
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
        if reply_type == "thread" and parent_id:
            parent_message = Message.get_or_none(id=parent_id)
            if parent_message:
                parent_message.last_reply_at = new_message.created_at
                parent_message.save()

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
            target_users_for_mention = set()

            # Handle @channel - all members
            if "@channel" in chat_text:
                all_members = (
                    User.select()
                    .join(ChannelMember)
                    .where(ChannelMember.channel == channel)
                )
                for member in all_members:
                    target_users_for_mention.add(member)

            # Handle @here - only online members
            if "@here" in chat_text:
                member_ids_query = ChannelMember.select(ChannelMember.user_id).where(
                    ChannelMember.channel == channel
                )
                member_ids = {m.user_id for m in member_ids_query}
                online_channel_members_ids = member_ids.intersection(
                    chat_manager.online_user_ids()
                )
                if online_channel_members_ids:
                    online_users = User.select().where(
                        User.id.in_(list(online_channel_members_ids))
                    )
                    for member in online_users:
                        target_users_for_mention.add(member)

            # 3. Create Mention records for the collected users
            for user_to_mention in target_users_for_mention:
                if user_to_mention.id != sender.id:
                    Mention.get_or_create(user=user_to_mention, message=new_message)

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
    """
    conversation = new_message.conversation
    conv_id_str = conversation.conversation_id_str
    parsed_conv = parse_conversation_id(conv_id_str)

    # Determine who the members of the conversation are
    if conversation.type == "channel":
        channel = Channel.get_by_id(parsed_conv.channel_id)
        members = (
            User.select().join(ChannelMember).where(ChannelMember.channel == channel)
        )
    else:  # DM
        members = User.select().where(User.id.in_(list(parsed_conv.user_ids)))
        # Guarantee both participants a status row even when they're offline.
        # The web sidebar and GET /api/v1/dms join through this table, so a
        # missing row hides the conversation entirely — the recipient never
        # saw the DM until they searched for the sender and started one
        # themselves. (The loop below get_or_creates too, but only after
        # skipping offline members.)
        for member in members:
            UserConversationStatus.get_or_create(user=member, conversation=conversation)

    # Loop through every member to see if they need a notification
    for member in members:
        # Condition 1: Don't notify the sender or any offline users (cluster-aware)
        if member.id == sender_user.id or not chat_manager.is_user_online_in_cluster(
            member.id
        ):
            continue

        # Condition 2: Check viewing status via 'exclude_channel' in send_to_user payload

        status, _ = UserConversationStatus.get_or_create(
            user=member, conversation=conversation
        )
        notification_html = None

        if conversation.type == "channel":
            channel_model = Channel.get_by_id(parsed_conv.channel_id)
            link_text = f"# {channel_model.name}"
            hx_get_url = url_for(
                "channels.get_channel_chat", channel_id=channel_model.id
            )

            is_mention = (
                Mention.select()
                .where((Mention.message == new_message) & (Mention.user == member))
                .exists()
            )

            if is_mention:
                total_unread_mentions = (
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
                    count=total_unread_mentions,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )
            elif (
                Message.select()
                .where(
                    (Message.conversation == conversation)
                    & (Message.created_at > status.last_read_timestamp)
                    & (Message.user != member)
                )
                .exists()
            ):
                notification_html = render_template(
                    "partials/bold_link.html",
                    conv_id_str=conv_id_str,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )
        else:  # DM
            link_text = sender_user.display_name or sender_user.username
            hx_get_url = url_for("dms.get_dm_chat", other_user_id=sender_user.id)
            new_count = (
                Message.select()
                .where(
                    (Message.conversation == conversation)
                    & (Message.created_at > status.last_read_timestamp)
                    & (Message.user != member)
                )
                .count()
            )
            if new_count > 0:
                # A sidebar row rides along with the badge: if this DM isn't in
                # the client's sidebar yet (brand-new conversation, or the
                # dm_created frame was lost while their socket was down), the
                # badge's OOB target doesn't exist and the whole notification
                # would be silently dropped. The client skips the row when it
                # already has one (htmx:oobBeforeSwap dedupe in chat.js).
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
                    count=new_count,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )

        if notification_html:
            unread_link_html = render_template("partials/unreads_link_unread.html")

            # Construct the json payload specifically for the mobile app
            api_data = {
                "type": "unread_updated",
                "data": {
                    "conversation_id_str": conv_id_str,
                    "unread_count": total_unread_mentions
                    if conversation.type == "channel" and is_mention
                    else (new_count if conversation.type == "dm" else 1),
                    "is_mention": bool(conversation.type == "channel" and is_mention),
                },
            }

            chat_manager.send_to_user(
                member.id,
                {"_raw_html": notification_html, "api_data": api_data},
                exclude_channel=conv_id_str,
            )
            chat_manager.send_to_user(
                member.id, unread_link_html, exclude_channel=conv_id_str
            )

        # Sound and Desktop Notification Logic (remains the same)
        now = utc_now()
        is_a_mention = (
            Mention.select()
            .where((Mention.message == new_message) & (Mention.user == member))
            .exists()
        )

        if is_a_mention:
            chat_manager.send_to_user(
                member.id, {"type": "sound"}, exclude_channel=conv_id_str
            )
            notification_payload = {
                "type": "notification",
                "title": f"New mention from {new_message.user.display_name or new_message.user.username}",
                "body": new_message.content,
                # Remove _external=True to use a safe relative path
                "icon": url_for("static", filename="favicon.ico"),
                "tag": conv_id_str,
            }
            chat_manager.send_to_user(
                member.id, notification_payload, exclude_channel=conv_id_str
            )
            status.last_notified_timestamp = now
            status.save()
        elif conversation.type == "dm":
            should_notify = status.last_notified_timestamp is None or (
                now - status.last_notified_timestamp
            ) > datetime.timedelta(seconds=10)
            if should_notify:
                chat_manager.send_to_user(
                    member.id, {"type": "sound"}, exclude_channel=conv_id_str
                )
                notification_payload = {
                    "type": "notification",
                    "title": f"New message from {new_message.user.display_name or new_message.user.username}",
                    "body": new_message.content,
                    # Remove _external=True to use a safe relative path
                    "icon": url_for("static", filename="favicon.ico"),
                    "tag": conv_id_str,
                }
                chat_manager.send_to_user(
                    member.id, notification_payload, exclude_channel=conv_id_str
                )
                status.last_notified_timestamp = now
                status.save()

    _dispatch_push_notifications(new_message, sender_user, conversation, parsed_conv)


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

    body = new_message.content or ""
    if len(body) > 240:
        body = body[:237] + "..."

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
