"""The Chief of Staff bot: D8's DMs to one person (D8 plan/CHIEF_OF_STAFF.md).

D8 sends ``POST /api/v1/internal/notify`` with ``{"to": "<email>",
"message": "<text>"}``; ``send_dm`` posts it as the ``chief-of-staff`` bot in
that person's DM with the bot, the way a person's DM is posted: they get the
unread badge, the sound and the desktop notification. Phase 1 sends the
morning brief this way. Replies to the bot are Phase 2.

The bot's DMs carry what each person has to do (candidates' names on the
interview steps, clients' on the invoices), so they are kept
``RETENTION_DAYS`` (90) and then deleted, replies and all, by
``purge_old_messages``. The on-call ticker runs it at most once an hour per
worker (``purge_if_due``); deleting is idempotent, so two workers racing
change nothing.
"""

from datetime import timedelta

from flask import current_app
from peewee import fn

from app.background import spawn_background
from app.models import Conversation, Message, User, WorkspaceMember, utc_now

BOT_USERNAME = "chief-of-staff"
BOT_EMAIL = "chief-of-staff@d8chat.local"
RETENTION_DAYS = 90
# Messages deleted per pass: an old backlog drains over a few passes rather
# than in one long transaction.
PURGE_BATCH = 500
PURGE_EVERY = timedelta(hours=1)

_last_purge = {"at": None}


def ensure_bot(workspace):
    """The ``chief-of-staff`` user D8's DMs come from. Idempotent."""
    bot, _ = User.get_or_create(
        username=BOT_USERNAME,
        defaults={
            "email": BOT_EMAIL,
            "display_name": "Chief of Staff",
            # Inactive: can never log in; only used as Message.user.
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(
        user=bot, workspace=workspace, defaults={"role": "member"}
    )
    return bot


def bot_user():
    return User.get_or_none(User.username == BOT_USERNAME)


def recipient(email):
    """The active user with ``email`` (case does not matter), or None."""
    return User.get_or_none(
        (fn.LOWER(User.email) == email.strip().lower()) & User.is_active
    )


def send_dm(person, text):
    """Post ``text`` to ``person`` from the bot, opening the DM if it is new.
    Returns the message, or None when the bot is not provisioned."""
    # pylint: disable=import-outside-toplevel,cyclic-import
    from app.routes import _broadcast_regular_message
    from app.services import chat_service

    bot = bot_user()
    if bot is None:
        current_app.logger.error("Cannot DM: the chief-of-staff bot is missing.")
        return None
    conv_id_str, conversation, created = chat_service.open_dm(bot, person)
    message = chat_service.handle_new_message(
        sender=bot, conversation=conversation, chat_text=text
    )
    if created:
        chat_service.announce_new_dm(bot, person, conv_id_str)
    _broadcast_regular_message(bot, message, conv_id_str)
    spawn_background(chat_service.send_notifications_for_new_message, message, bot)
    return message


def _bot_dm_conversations(bot):
    """The bot's DM conversations: ``dm_<a>_<b>`` with the bot as a or b."""
    ids = str(bot.id)
    return Conversation.select().where(
        (Conversation.type == "dm")
        & (
            Conversation.conversation_id_str.startswith(f"dm_{ids}_")
            | Conversation.conversation_id_str.endswith(f"_{ids}")
        )
    )


def purge_old_messages(now=None):
    """Delete the bot's DM messages older than RETENTION_DAYS, the person's
    replies included, through the one delete path every message takes
    (uploads that are theirs alone go too; a thread's first message stays as
    an empty placeholder while its replies remain). Returns how many."""
    # pylint: disable=import-outside-toplevel,cyclic-import
    from app.services import chat_service

    bot = bot_user()
    if bot is None:
        return 0
    cutoff = (now or utc_now()) - timedelta(days=RETENTION_DAYS)
    old = (
        Message.select()
        .where(
            Message.conversation.in_(_bot_dm_conversations(bot))
            & (Message.created_at < cutoff)
            & (Message.is_deleted == False)  # noqa: E712 (peewee expression)
        )
        .order_by(Message.created_at.desc())
        .limit(PURGE_BATCH)
    )
    count = 0
    # Newest first: a thread's replies go before the message that started
    # it, so the starter is deleted outright rather than left a placeholder.
    for message in list(old):
        chat_service.delete_message_and_files(message)
        count += 1
    return count


def purge_if_due(now=None):
    """Run ``purge_old_messages`` if this worker hasn't in PURGE_EVERY."""
    now = now or utc_now()
    last = _last_purge["at"]
    if last is not None and now - last < PURGE_EVERY:
        return 0
    _last_purge["at"] = now
    return purge_old_messages(now)
