# app/services/chat_service.py

import datetime
import re

from app.chat_manager import chat_manager
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
        if attachment_file_ids:
            file_ids = [
                int(id) for id in attachment_file_ids.split(",") if id.isdigit()
            ]
            for file_id in file_ids:
                MessageAttachment.create(message=new_message, attachment=file_id)

        # --- Mention handling logic ---
        # 1. Handle regular @username mentions
        mentioned_usernames = set(re.findall(r"@(\w+)", chat_text))
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
            channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
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
                    chat_manager.online_users.keys()
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
        hashtag_pattern = r"(?<!#)#([a-zA-Z0-9_-]+)"
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

    # Determine who the members of the conversation are
    if conversation.type == "channel":
        channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
        members = (
            User.select().join(ChannelMember).where(ChannelMember.channel == channel)
        )
    else:  # DM
        user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
        members = User.select().where(User.id.in_(user_ids))

    # Loop through every member to see if they need a notification
    for member in members:
        # Condition 1: Don't notify the sender or any offline users
        if member.id == sender_user.id or member.id not in chat_manager.all_clients:
            continue

        member_ws = chat_manager.all_clients[member.id]

        # Condition 2: Don't notify users who are currently viewing this exact conversation
        is_viewing_conversation = (
            hasattr(member_ws, "channel_id") and member_ws.channel_id == conv_id_str
        )
        if is_viewing_conversation:
            continue

        # If we get here, the user is online but in a different channel/DM.
        # They need a UI update.
        from flask import render_template, url_for

        status, _ = UserConversationStatus.get_or_create(
            user=member, conversation=conversation
        )
        notification_html = None

        if conversation.type == "channel":
            channel_model = Channel.get_by_id(
                conversation.conversation_id_str.split("_")[1]
            )
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
                notification_html = render_template(
                    "partials/unread_badge.html",
                    conv_id_str=conv_id_str,
                    count=new_count,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )

        if notification_html:
            unread_link_html = render_template("partials/unreads_link_unread.html")
            chat_manager.send_to_user(member.id, notification_html)
            chat_manager.send_to_user(member.id, unread_link_html)

        # Sound and Desktop Notification Logic (remains the same)
        now = datetime.datetime.now()
        is_a_mention = (
            Mention.select()
            .where((Mention.message == new_message) & (Mention.user == member))
            .exists()
        )

        if is_a_mention:
            chat_manager.send_to_user(member.id, {"type": "sound"})
            notification_payload = {
                "type": "notification",
                "title": f"New mention from {new_message.user.display_name or new_message.user.username}",
                "body": new_message.content,
                "icon": url_for("static", filename="favicon.ico", _external=True),
                "tag": conv_id_str,
            }
            chat_manager.send_to_user(member.id, notification_payload)
            status.last_notified_timestamp = now
            status.save()
        elif conversation.type == "dm":
            should_notify = status.last_notified_timestamp is None or (
                now - status.last_notified_timestamp
            ) > datetime.timedelta(seconds=10)
            if should_notify:
                chat_manager.send_to_user(member.id, {"type": "sound"})
                notification_payload = {
                    "type": "notification",
                    "title": f"New message from {new_message.user.display_name or new_message.user.username}",
                    "body": new_message.content,
                    "icon": url_for("static", filename="favicon.ico", _external=True),
                    "tag": conv_id_str,
                }
                chat_manager.send_to_user(member.id, notification_payload)
                status.last_notified_timestamp = now
                status.save()
