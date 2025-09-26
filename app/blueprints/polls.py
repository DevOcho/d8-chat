# app/blueprints/polls.py
from flask import Blueprint, g, make_response, render_template, request

from app.chat_manager import chat_manager
from app.models import Conversation, Message, Poll, PollOption, Vote, db
from app.routes import (
    get_attachments_for_messages,
    get_reactions_for_messages,
    login_required,
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
    conversation = Conversation.get(conversation_id_str=conv_id_str)

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
    broadcast_html = (
        f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
    )

    # Broadcast the new poll to everyone in the channel
    chat_manager.broadcast(conv_id_str, broadcast_html, sender_ws=None)

    # Return a response that closes the modal for the creator
    response = make_response()
    response.headers["HX-Trigger"] = "close-modal, focus-chat-input"
    return response


@polls_bp.route("/chat/poll/option/<int:option_id>/vote", methods=["POST"])
@login_required
def vote_on_poll(option_id):
    """Handles a user voting on a poll option."""
    option = PollOption.get_or_none(id=option_id)
    if not option:
        return "Poll option not found", 404

    poll = option.poll
    message = poll.message

    with db.atomic():
        # Find if the user has already voted on any option in this poll
        existing_vote = (
            Vote.select()
            .join(PollOption)
            .where((Vote.user == g.user) & (PollOption.poll == poll))
            .first()
        )

        if existing_vote:
            # If they clicked the same option they already voted for, it's an "un-vote"
            if existing_vote.option.id == option.id:
                existing_vote.delete_instance()
            else:
                # If they clicked a different option, switch their vote
                existing_vote.option = option
                existing_vote.save()
        else:
            # If they haven't voted yet, create a new vote
            Vote.create(user=g.user, option=option)

    # Get the fresh poll data for rendering
    poll_context = get_poll_context(poll, g.user)

    # --- For the user who voted ---
    # Render the full poll partial. This will switch their view to the "voted" state.
    response_for_voter = render_template(
        "partials/message_poll.html", poll_context=poll_context
    )

    # --- For everyone else ---
    # Render the OOB update partial. This will only update the numbers for users
    # who have already voted and can see the results.
    broadcast_html = render_template(
        "partials/message_poll_oob_update.html", poll_context=poll_context
    )
    chat_manager.broadcast(
        message.conversation.conversation_id_str, broadcast_html, sender_ws=None
    )

    # Return the full updated partial directly to the user who voted
    return response_for_voter
