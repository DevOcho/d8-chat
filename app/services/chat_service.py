# app/services/chat_service.py

from app.models import db, Message, Mention, User, Channel, ChannelMember, Conversation
from app.chat_manager import chat_manager
import re


def handle_new_message(
    sender: User, conversation: Conversation, chat_text: str, parent_id: int = None
):
    """
    Handles the business logic for creating a new message and its associated mentions.

    Args:
        sender: The User object of the message sender.
        conversation: The Conversation object where the message was sent.
        chat_text: The raw content of the message.
        parent_id: The ID of the parent message, if it's a reply.

    Returns:
        The newly created Message object.
    """
    with db.atomic():
        new_message = Message.create(
            user=sender,
            conversation=conversation,
            content=chat_text,
            parent_message=parent_id if parent_id else None,
        )

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

    return new_message
