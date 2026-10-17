"""Account lifecycle shared by the admin UI and the Authentik sync."""

from app.chat_manager import chat_manager
from app.models import DeviceToken, db

DEACTIVATED_REASON = "Account deactivated"


def deactivate_user(user):
    """Revoke all of ``user``'s access while preserving their history.

    Sets ``is_active = False``, which ``User.get_active_by_id`` enforces at
    every login, session, API-token, SSO and WebSocket-frame entry point;
    deletes their push device tokens; and closes their open sockets on every
    worker. Returns False if they were already inactive.

    Never a hard delete: messages, reactions, mentions, votes and uploaded
    files all foreign-key back to the user row.
    """
    if not user.is_active:
        return False
    with db.atomic():
        user.is_active = False
        user.save()
        DeviceToken.delete().where(DeviceToken.user == user).execute()
    chat_manager.disconnect_user(user.id, DEACTIVATED_REASON)
    return True
