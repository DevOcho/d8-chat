# app/blueprints/polls.py
from flask import Blueprint, g, make_response, render_template, request

from app.access import user_can_access_message, user_can_post
from app.chat_manager import chat_manager
from app.conversation_id import parse_conversation_id
from app.htmx_oob import message_list_append_oob
from app.models import (
    Conversation,
    Message,
    Poll,
    PollOption,
    Vote,
    db,
    lock_for_update,
)
from app.routes import (
    date_separator_oob,
    get_attachments_for_messages,
    get_reactions_for_messages,
    login_required,
    previous_visible_message,
)

polls_bp = Blueprint("polls", __name__)


def get_poll_context(poll, current_user):
    """
    Gathers all necessary data to render a poll's state, including options,
    vote counts, total votes, and the current user's vote.
    """
    options_with_votes = []
    user_vote_option_id = None

    # Find the user's current vote for any option in this specific poll
    user_vote = (
        Vote.select(Vote.option_id)
        .join(PollOption)
        .where((Vote.user == current_user) & (PollOption.poll == poll))
        .scalar()
    )

    if user_vote:
        user_vote_option_id = user_vote

    total_votes = 0
    # Loop through the poll's options to gather details for each one
    for option in poll.options.order_by(PollOption.id):
        vote_count = option.votes.count()
        total_votes += vote_count
        options_with_votes.append(
            {"id": option.id, "text": option.text, "count": vote_count}
        )

    return {
        "poll": poll,
        "options": options_with_votes,
        "total_votes": total_votes,
        "user_vote_option_id": user_vote_option_id,
    }


@polls_bp.route("/chat/poll/create_form", methods=["GET"])
@login_required
def get_create_poll_form():
    """Renders the modal content for creating a new poll."""
    return render_template("partials/create_poll_modal.html")


@polls_bp.route("/chat/poll/create", methods=["POST"])
@login_required
def create_poll():
    """Handles the submission of the poll creation form."""
    question = request.form.get("question", "").strip()
    options = [opt.strip() for opt in request.form.getlist("options[]") if opt.strip()]

    if not question or len(options) < 2:
        # Re-render the form with an error if validation fails
        return render_template(
            "partials/create_poll_modal.html",
            error="A question and at least two options are required.",
            question=question,
            options=options,
        )

    # Get the current conversation from the hidden input in the main chat form
    conv_id_str = request.form.get("conversation_id_str")
    if not conv_id_str:
        return "Could not determine the current conversation.", 400
    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
    if conversation is None:
        return "Conversation not found.", 404
    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return "Malformed conversation id.", 400
    if not user_can_post(g.user, parsed):
        return "You can't post to this conversation.", 403

    with db.atomic():
        # Create a message to act as the container for our poll
        poll_message = Message.create(
            user=g.user,
            conversation=conversation,
            content=f"[Poll]: {question}",  # Fallback content
        )
        # Create the poll itself, linking it to the message
        new_poll = Poll.create(message=poll_message, question=question)
        # Create the options for the poll
        for option_text in options:
            PollOption.create(poll=new_poll, text=option_text)

    # Now, render the new message containing the poll to broadcast it
    reactions_map = get_reactions_for_messages([poll_message])
    attachments_map = get_attachments_for_messages([poll_message])
    new_message_html = render_template(
        "partials/message.html",
        message=poll_message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )
    broadcast_html = date_separator_oob(
        poll_message, previous_visible_message(poll_message), conv_id_str
    ) + message_list_append_oob(conv_id_str, new_message_html)

    # Broadcast the new poll to everyone in the channel. Include api_data so
    # mobile clients receive the poll as a structured new_message instead of raw
    # HTML they can't render.
    from app.blueprints.api_v1 import serialize_message

    api_data = {
        "type": "new_message",
        "data": serialize_message(poll_message, reactions_map, attachments_map),
    }
    chat_manager.broadcast(
        conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
    )

    # Return a response that closes the modal for the creator
    response = make_response()
    response.headers["HX-Trigger"] = "close-modal, focus-chat-input"
    return response


def cast_vote(user, poll, option):
    """Vote for ``option``, switch to it, or withdraw the vote when it's the
    option already chosen. Shared by the web and API vote routes."""
    with db.atomic():
        # Serialize votes on this poll: two racing clicks on different options
        # each saw no vote and both inserted, two votes in a single-choice poll.
        lock_for_update(Poll.select().where(Poll.id == poll.id)).execute()
        existing_vote = (
            Vote.select()
            .join(PollOption)
            .where((Vote.user == user) & (PollOption.poll == poll))
            .first()
        )
        if existing_vote:
            # Option is part of Vote's composite primary key, so a switch is a
            # delete and a create rather than an update.
            existing_vote.delete_instance()
            if existing_vote.option.id == option.id:
                return
        Vote.create(user=user, option=option)


def announce_votes(poll, voter):
    """Tell everyone in the poll's conversation its tallies changed, however
    the vote was cast: web clients get the tally OOB update, mobile clients the
    poll's message as ``message_edited`` (the same frame for a web vote and an
    API vote). The frame carries no ``voted_option_id``: ChatManager drops it
    from every frame, since the voter's choice isn't anyone else's.

    Returns ``(poll_context, message_data)`` from the voter's point of view,
    for the route's own response.
    """
    from app.blueprints.api_v1 import serialize_message

    message = poll.message
    conv_id_str = message.conversation.conversation_id_str
    poll_context = get_poll_context(poll, voter)
    broadcast_html = render_template(
        "partials/message_poll_oob_update.html", poll_context=poll_context
    )
    message_data = serialize_message(
        message,
        get_reactions_for_messages([message]),
        get_attachments_for_messages([message]),
    )
    chat_manager.broadcast(
        conv_id_str,
        {
            "_raw_html": broadcast_html,
            "api_data": {"type": "message_edited", "data": message_data},
        },
    )
    return poll_context, message_data


@polls_bp.route("/chat/poll/option/<int:option_id>/vote", methods=["POST"])
@login_required
def vote_on_poll(option_id):
    """Handles a user voting on a poll option."""
    option = PollOption.get_or_none(id=option_id)
    if not option:
        return "Poll option not found", 404

    poll = option.poll
    message = poll.message
    if not user_can_access_message(g.user, message):
        return "Unauthorized", 403

    cast_vote(g.user, poll, option)
    poll_context, _ = announce_votes(poll, g.user)

    # The voter's own poll switches to the "voted" state; everyone else got
    # the tallies from announce_votes.
    response_for_voter = render_template(
        "partials/message_poll.html", poll_context=poll_context
    )

    # Return the full updated partial directly to the user who voted
    return response_for_voter
