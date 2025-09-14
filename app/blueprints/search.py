# app/blueprints/search.py

from flask import Blueprint, render_template, request, g
from peewee import fn
from ..models import (
    Message,
    User,
    Channel,
    ChannelMember,
    Conversation,
    Hashtag,
    MessageHashtag,
    UserConversationStatus,
)
from ..routes import login_required

search_bp = Blueprint("search", __name__)

SEARCH_PAGE_SIZE = 20


def _get_accessible_conversations(user):
    """
    Helper function to build a query that returns all Conversation
    objects a given user has access to (their DMs and channels).
    """
    user_channels_query = (
        Channel.select(Channel.id).join(ChannelMember).where(ChannelMember.user == user)
    )
    channel_convs_query = (
        Conversation.select(Conversation.id)
        .join(
            Channel,
            on=(Conversation.conversation_id_str == fn.CONCAT("channel_", Channel.id)),
        )
        .where(Channel.id.in_(user_channels_query))
    )
    dm_convs_query = (
        Conversation.select(Conversation.id)
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == user) & (Conversation.type == "dm"))
    )
    return channel_convs_query | dm_convs_query


def _get_message_context(messages, current_user):
    """
    Efficiently fetches the context (channel name or DM partner) for a list of messages.
    Returns a dictionary mapping message_id to its context string.
    """
    context_map = {}
    if not messages:
        return context_map

    # 1. Collect all the unique conversation IDs and the entities we need to look up.
    channel_ids_to_find = set()
    dm_partner_ids_to_find = set()
    for msg in messages:
        conv_str = msg.conversation.conversation_id_str
        if msg.conversation.type == "channel":
            channel_ids_to_find.add(int(conv_str.split("_")[1]))
        elif msg.conversation.type == "dm":
            user_ids = [int(uid) for uid in conv_str.split("_")[1:]]
            partner_id = next(
                (uid for uid in user_ids if uid != current_user.id), current_user.id
            )
            dm_partner_ids_to_find.add(partner_id)

    # 2. Fetch all required entities in one query for channels and one for users.
    channel_lookup = {
        c.id: c.name
        for c in Channel.select().where(Channel.id.in_(list(channel_ids_to_find)))
    }
    user_lookup = {
        u.id: (u.display_name or u.username)
        for u in User.select().where(User.id.in_(list(dm_partner_ids_to_find)))
    }

    # 3. Build the final context map for the template.
    for msg in messages:
        conv_str = msg.conversation.conversation_id_str
        if msg.conversation.type == "channel":
            channel_id = int(conv_str.split("_")[1])
            context_map[
                msg.id
            ] = f"# {channel_lookup.get(channel_id, 'unknown-channel')}"
        elif msg.conversation.type == "dm":
            user_ids = [int(uid) for uid in conv_str.split("_")[1:]]
            partner_id = next(
                (uid for uid in user_ids if uid != current_user.id), current_user.id
            )
            partner_name = user_lookup.get(partner_id, "Unknown User")
            # Handle DMs with self
            if partner_id == current_user.id:
                context_map[msg.id] = f"{partner_name} (you)"
            else:
                context_map[msg.id] = partner_name

    return context_map


@search_bp.route("/chat/search", methods=["GET"])
@login_required
def search():
    """
    Performs a global search across messages, channels, and users,
    and returns the initial search results panel.
    """
    query = request.args.get("q", "").strip()
    if not query:
        return '<div id="search-results-content"></div>'

    accessible_convs_query = _get_accessible_conversations(g.user)

    # Check if this is a hashtag search
    if query.startswith("#"):
        hashtag_name = query[1:]
        message_query = (
            Message.select(Message, User, Conversation)
            .join(User)
            .switch(Message)
            .join(Conversation)
            .switch(Message)
            .join(MessageHashtag)
            .join(Hashtag)
            .where(
                (Hashtag.name == hashtag_name)
                & (Message.conversation.in_(accessible_convs_query))
            )
            .order_by(Message.created_at.desc())
        )
        # For hashtag searches, we don't need to search channels or users
        channel_count = 0
        user_count = 0
    else:
        # This is the original logic for general text search
        message_query = (
            Message.select(Message, User, Conversation)
            .join(User)
            .switch(Message)
            .join(Conversation)
            .where(
                Message.content.ilike(f"%{query}%"),
                Message.conversation.in_(accessible_convs_query),
            )
            .order_by(Message.created_at.desc())
        )
        user_private_channels_subquery = (
            Channel.select(Channel.id)
            .join(ChannelMember)
            .where((ChannelMember.user == g.user) & (Channel.is_private == True))
        )
        channel_query = Channel.select().where(
            (Channel.name.ilike(f"%{query}%"))
            & (
                (Channel.is_private == False)
                | (Channel.id.in_(user_private_channels_subquery))
            )
        )
        channel_count = channel_query.count()

        user_query = User.select().where(
            (User.username.ilike(f"%{query}%"))
            | (User.display_name.ilike(f"%{query}%"))
        )
        user_count = user_query.count()

    message_count = message_query.count()
    message_results = list(message_query.limit(SEARCH_PAGE_SIZE))

    # --- Get context for the message results ---
    message_context = _get_message_context(message_results, g.user)

    return render_template(
        "partials/search_results.html",
        query=query,
        messages=message_results,
        message_count=message_count,
        channel_count=channel_count,
        user_count=user_count,
        has_more_messages=message_count > SEARCH_PAGE_SIZE,
        current_page=1,
        message_context=message_context,  # Pass context to template
    )


@search_bp.route("/chat/search/messages", methods=["GET"])
@login_required
def search_messages_paginated():
    """Handles paginated requests for message search results."""
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    accessible_convs_query = _get_accessible_conversations(g.user)

    if query.startswith("#"):
        hashtag_name = query[1:]
        message_query = (
            Message.select(Message, User, Conversation)
            .join(User)
            .switch(Message)
            .join(Conversation)
            .switch(Message)
            .join(MessageHashtag)
            .join(Hashtag)
            .where(
                (Hashtag.name == hashtag_name)
                & (Message.conversation.in_(accessible_convs_query))
            )
            .order_by(Message.created_at.desc())
        )
    else:
        message_query = (
            Message.select(Message, User, Conversation)
            .join(User)
            .switch(Message)
            .join(Conversation)
            .where(
                Message.content.ilike(f"%{query}%"),
                Message.conversation.in_(accessible_convs_query),
            )
            .order_by(Message.created_at.desc())
        )

    total_count = message_query.count()
    messages = list(message_query.paginate(page, SEARCH_PAGE_SIZE))
    has_more = total_count > (page * SEARCH_PAGE_SIZE)

    # --- Get context for the paginated message results ---
    message_context = _get_message_context(messages, g.user)

    return render_template(
        "partials/search_results_messages.html",
        query=query,
        messages=messages,
        has_more_messages=has_more,
        current_page=page,
        message_context=message_context,  # Pass context to template
    )


@search_bp.route("/chat/search/channels", methods=["GET"])
@login_required
def search_channels_paginated():
    """Handles paginated requests for channel search results."""
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    user_private_channels_subquery = (
        Channel.select(Channel.id)
        .join(ChannelMember)
        .where((ChannelMember.user == g.user) & (Channel.is_private == True))
    )
    channel_query = (
        Channel.select()
        .where(
            (Channel.name.ilike(f"%{query}%"))
            & (
                (Channel.is_private == False)
                | (Channel.id.in_(user_private_channels_subquery))
            )
        )
        .order_by(Channel.name)
    )
    total_count = channel_query.count()
    channels = channel_query.paginate(page, SEARCH_PAGE_SIZE)
    has_more = total_count > (page * SEARCH_PAGE_SIZE)

    return render_template(
        "partials/search_results_channels.html",
        query=query,
        channels=channels,
        has_more_channels=has_more,
        current_page=page,
    )


@search_bp.route("/chat/search/users", methods=["GET"])
@login_required
def search_users_paginated():
    """Handles paginated requests for user search results."""
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    user_query = (
        User.select()
        .where(
            (User.username.ilike(f"%{query}%"))
            | (User.display_name.ilike(f"%{query}%"))
        )
        .order_by(User.username)
    )
    total_count = user_query.count()
    users = user_query.paginate(page, SEARCH_PAGE_SIZE)
    has_more = total_count > (page * SEARCH_PAGE_SIZE)

    return render_template(
        "partials/search_results_users.html",
        query=query,
        users=users,
        has_more_users=has_more,
        current_page=page,
    )
