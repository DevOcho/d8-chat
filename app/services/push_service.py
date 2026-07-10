"""Mobile push notification dispatch via Firebase Cloud Messaging.

FCM handles delivery to both Android and (via Apple's APNs bridge) iOS, so
the entire push pipeline goes through one SDK. iOS pushes require the
project's APNs ``.p8`` key to be uploaded into Firebase project settings —
no code change is needed when that lands; this module just starts seeing
iOS tokens succeed instead of returning ``UNREGISTERED``.

Designed to fail open at every layer. Self-hosters without Firebase
credentials see a no-op (so the chat keeps working without push); Firebase
delivery errors are logged and dropped (so a transient FCM outage doesn't
poison a request); and stale tokens are pruned on the same response so a
deactivated device stops eating delivery attempts.
"""

# pylint: disable=import-outside-toplevel

import logging
import os

logger = logging.getLogger(__name__)

# Set during init_app when FIREBASE_CREDENTIALS_PATH is configured. When
# unset, every public helper is a no-op.
_firebase_app = None


def init_app(app):
    """Initialize firebase-admin from ``FIREBASE_CREDENTIALS_PATH``.

    No-op when the config var is unset — push notifications stay disabled
    and the rest of the chat still works. This is the path self-hosters
    will see by default until they bring their own Firebase project.
    """
    global _firebase_app

    # Init status logged at WARNING so it's always visible in dev logs
    # (Flask's default app.logger suppresses INFO in debug mode), making
    # "is push live?" a glance at startup rather than a Python REPL trip.
    cred_path = app.config.get("FIREBASE_CREDENTIALS_PATH")
    if not cred_path:
        app.logger.warning(
            "FIREBASE_CREDENTIALS_PATH not set; push notifications disabled."
        )
        return

    # The credentials are mounted from an optional k8s Secret, so in dev (and
    # any deploy without the Secret) the path is set but the file is absent.
    # Degrade quietly instead of letting credentials.Certificate() raise a
    # FileNotFoundError traceback on every startup.
    if not os.path.exists(cred_path):
        app.logger.warning(
            "FIREBASE_CREDENTIALS_PATH set to %s but no file is present; "
            "push notifications disabled.",
            cred_path,
        )
        return

    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        app.logger.warning(
            "firebase-admin is not installed; push notifications disabled."
        )
        return

    try:
        cred = credentials.Certificate(cred_path)
        # Name the app so re-init under the same process (e.g. test
        # teardown/setup) doesn't raise ValueError on duplicate default.
        _firebase_app = firebase_admin.initialize_app(cred, name="d8-chat-push")
        app.logger.warning("Firebase Admin initialized for push notifications.")
    except Exception:
        # Bad credentials path / corrupt JSON / unreachable Firebase: log
        # loudly but don't take down the app.
        app.logger.exception("Failed to initialize Firebase Admin; push disabled.")
        _firebase_app = None


def is_configured() -> bool:
    """True when Firebase init succeeded and pushes will actually be sent."""
    return _firebase_app is not None


def send_to_user(user_id, *, title, body, data=None):
    """Send a push to every registered device for ``user_id``.

    Silently no-ops when Firebase isn't configured or the user has no
    registered tokens. Stale tokens (``UNREGISTERED`` /
    ``INVALID_ARGUMENT``) are deleted from the DB on the same response so
    we stop trying to deliver to them.

    ``data`` should contain ``conversation_id_str`` and ``message_id`` so
    the mobile app can deep-link from the notification tap back to the
    right conversation.
    """
    if not is_configured():
        return

    # Local imports keep this module importable in environments where
    # firebase-admin isn't installed (e.g. tests that mock the whole module).
    from app.models import DeviceToken, utc_now

    tokens = list(
        DeviceToken.select(DeviceToken.id, DeviceToken.token).where(
            DeviceToken.user == user_id
        )
    )
    if not tokens:
        return

    token_strings = [t.token for t in tokens]
    payload_data = {str(k): str(v) for k, v in (data or {}).items()}

    try:
        from firebase_admin import messaging
    except ImportError:
        logger.warning("firebase-admin missing at send time; skipping push.")
        return

    message = messaging.MulticastMessage(
        tokens=token_strings,
        notification=messaging.Notification(title=title, body=body),
        data=payload_data,
    )

    try:
        response = messaging.send_each_for_multicast(message, app=_firebase_app)
    except Exception:
        logger.exception("FCM send failed for user_id=%s", user_id)
        return

    now = utc_now()
    stale_ids = []
    for idx, resp in enumerate(response.responses):
        if resp.success:
            tokens[idx].last_used_at = now
            tokens[idx].save()
            continue

        exc = resp.exception
        if exc is None:
            continue
        # firebase-admin exposes the FCM error code on the exception class
        # name (UnregisteredError) or .code attribute depending on version.
        code = getattr(exc, "code", "") or exc.__class__.__name__
        if any(
            marker in str(code).upper()
            for marker in ("UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND")
        ):
            stale_ids.append(tokens[idx].id)
        else:
            logger.warning(
                "FCM error for user_id=%s token_id=%s: %s",
                user_id,
                tokens[idx].id,
                exc,
            )

    if stale_ids:
        DeviceToken.delete().where(DeviceToken.id.in_(stale_ids)).execute()
        logger.info(
            "Pruned %d stale FCM token(s) for user_id=%s", len(stale_ids), user_id
        )
