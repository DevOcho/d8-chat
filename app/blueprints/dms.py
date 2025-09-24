# app/blueprints/dms.py

import datetime

from flask import Blueprint, g, make_response, render_template, request, url_for

from app.chat_manager import chat_manager
from app.models import Conversation, Message, User, UserConversationStatus
from app.routes import (
    PAGE_SIZE,
    check_and_get_read_state_oob,
    get_attachments_for_messages,
    get_reactions_for_messages,
    login_required,
)

# A smaller page size for the user search modal
DM_SEARCH_PAGE_SIZE = 20

dms_bp = Blueprint("dms", __name__)


@dms_bp.route("/chat/dms/start", methods=["GET"])
@login_required
def get_start_dm_form():
    """
    Renders the modal for starting a new DM, showing the first page of available users.
    """
    # Local Variables
    page = 1

    # Get the IDs of users the current user ALREADY has a DM with.
    dm_conversations = (
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )
    existing_partner_ids = {g.user.id}
    for conv in dm_conversations:
        user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
        partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
        if partner_id:
            existing_partner_ids.add(partner_id)

    # Base query for users not already in a DM, ordered alphabetically
    query = (
        User.select()
        .where(User.id.not_in(list(existing_partner_ids)))
        .order_by(User.username)
    )

    # Paginate the results
    total_users = query.count()
    users_for_page = query.paginate(page, DM_SEARCH_PAGE_SIZE)
    has_more_pages = total_users > (page * DM_SEARCH_PAGE_SIZE)

    # Render the main modal shell, which includes the first page of results.
    return render_template(
        "partials/start_dm_modal.html",
        users_to_start_dm=users_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@dms_bp.route("/chat/dms/search", methods=["GET"])
@login_required
def search_users_for_dm():
    """
    Searches for users to start a new DM with, supporting pagination.
    """
    search_term = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    # Find users already in DMs to exclude them from search.
    dm_conversations = (
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )
    existing_partner_ids = {g.user.id}
    for conv in dm_conversations:
        user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
        partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
        if partner_id:
            existing_partner_ids.add(partner_id)

    # Base query for users not already in a DM.
    query = User.select().where(User.id.not_in(list(existing_partner_ids)))

    # Apply search filter if a query is provided.
    if search_term:
        query = query.where(
            (User.username.contains(search_term))
            | (User.display_name.contains(search_term))
        )

    # Get the next (or first) batch of users
    total_users = query.count()
    users_for_page = query.order_by(User.username).paginate(page, DM_SEARCH_PAGE_SIZE)
    has_more_pages = total_users > (page * DM_SEARCH_PAGE_SIZE)

    # This will load the users with the load more button setup for the next batch
    return render_template(
        "partials/dm_user_results.html",
        users_to_start_dm=users_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@dms_bp.route("/chat/dm/<int:other_user_id>")
@login_required
def get_dm_chat(other_user_id):
    # Local Vars
    clear_badge_html = ""
    add_to_sidebar_html = ""

    # If there isn't the other user we can't open the DM
    other_user = User.get_or_none(id=other_user_id)
    if not other_user:
        return "User not found", 404

    # Make sure there are conversation records for both users
    user_ids = sorted([g.user.id, other_user.id])
    conv_id_str = f"dm_{user_ids[0]}_{user_ids[1]}"
    conversation, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str, defaults={"type": "dm"}
    )

    # Ensure a conversation status record exists for both users.
    status, created = UserConversationStatus.get_or_create(
        user=g.user, conversation=conversation
    )
    UserConversationStatus.get_or_create(user=other_user, conversation=conversation)

    # This is the timestamp of the last message the current user has seen.
    last_read_timestamp = status.last_read_timestamp

    # Now, update the timestamp for ONLY the current user to mark messages as read.
    status.last_read_timestamp = datetime.datetime.now()
    status.save()

    messages = list(
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )
    messages.reverse()
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)

    header_html_content = render_template(
        "partials/dm_header.html", other_user=other_user
    )
    header_html = f'<div id="chat-header-container" hx-swap-oob="true">{header_html_content}</div>'

    messages_html = render_template(
        "partials/dm_messages.html",
        messages=messages,
        other_user=other_user,
        last_read_timestamp=last_read_timestamp,
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )

    if not created and other_user.id != g.user.id:
        clear_badge_html = render_template(
            "partials/clear_badge.html",
            conv_id_str=conv_id_str,
            hx_get_url=url_for("dms.get_dm_chat", other_user_id=other_user.id),
            link_text=other_user.display_name or other_user.username,
        )
    elif created and other_user.id != g.user.id:
        # [THE FIX] This block now renders the correct partial for the initiator
        add_to_sidebar_html = render_template(
            "partials/dm_list_item_oob.html",
            user=other_user,
            conv_id_str=conv_id_str,
            is_online=other_user.id in chat_manager.online_users,
        )

        if other_user.id in chat_manager.all_clients:
            try:
                recipient_ws = chat_manager.all_clients[other_user.id]
                # And this renders the correct partial for the recipient
                new_contact_html = render_template(
                    "partials/dm_list_item_oob.html",
                    user=g.user,
                    conv_id_str=conv_id_str,
                    is_online=g.user.id in chat_manager.online_users,
                )
                recipient_ws.send(new_contact_html)
            except Exception as e:
                print(f"Could not send real-time DM add to user {other_user.id}: {e}")

    chat_input_html = render_template("partials/chat_input_default.html")
    chat_input_oob_html = f'<div id="chat-input-container" hx-swap-oob="outerHTML">{chat_input_html}</div>'

    read_state_oob_html = check_and_get_read_state_oob(g.user, conversation)

    full_response = (
        messages_html
        + header_html
        + clear_badge_html
        + add_to_sidebar_html
        + chat_input_oob_html
        + read_state_oob_html
    )
    response = make_response(full_response)
    return response


@dms_bp.route("/chat/dm/<int:other_user_id>/details", methods=["GET"])
@login_required
def get_dm_details(other_user_id):
    """Renders the details panel for a direct message conversation."""
    other_user = User.get_or_none(id=other_user_id)
    if not other_user:
        return "User not found", 404

    # Get the context, defaulting to 'dm' if not provided
    context = request.args.get("context", "dm")

    response = make_response(
        render_template(
            "partials/dm_details.html", other_user=other_user, context=context
        )
    )
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


@dms_bp.route("/chat/dm/<int:other_user_id>/leave", methods=["DELETE"])
@login_required
def leave_dm(other_user_id):
    """
    'Leaves' a DM by deleting the UserConversationStatus for the current user.
    """
    # Find the conversation
    user_ids = sorted([g.user.id, other_user_id])
    conv_id_str = f"dm_{user_ids[0]}_{user_ids[1]}"
    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)

    # Delete the status record for the current user, effectively hiding the DM
    if conversation:
        (
            UserConversationStatus.delete()
            .where(
                (UserConversationStatus.user == g.user)
                & (UserConversationStatus.conversation == conversation)
            )
            .execute()
        )

    # Put them back on the (you) chat
    response = make_response("")
    response.headers["HX-Redirect"] = url_for("main.chat_interface")
    return response
