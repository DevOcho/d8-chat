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
import time

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

    def build(messaging, token_strings, payload_data):
        return messaging.MulticastMessage(
            tokens=token_strings,
            notification=messaging.Notification(title=title, body=body),
            data=payload_data,
        )

    _send(user_id, data, build)


# Android notification channel the mobile app registers for on-call pages
# (high importance, alarm sound). Sent on every page so the app can route it
# without inspecting the payload.
PAGE_ANDROID_CHANNEL_ID = "oncall_pages"

# iOS sound for pages, bundled by the mobile app. iOS plays the default alert
# sound when the app doesn't ship a file by this name, so this is safe to send
# before the app has it.
PAGE_IOS_SOUND = "pager.caf"

# iOS notification category on pages. The app registers it with an
# Acknowledge action; iOS ignores a category the app hasn't registered.
PAGE_IOS_CATEGORY = "ONCALL_PAGE"

# How long FCM / APNs hold an undelivered page. By default they keep a push for
# weeks, so a phone that was off would ring for a page that ended long ago once
# it reconnects. Active pages repeat about every 60 s, so 120 s loses nothing.
PAGE_TTL_SECONDS = 120


def send_page(user_id, *, title, body, data=None):
    """Send an on-call page: the most intrusive push the platforms allow.

    Android: high-priority message on the ``oncall_pages`` channel with max
    notification priority. iOS: priority-10 critical alert — it only breaks
    through silent / Do Not Disturb once Apple grants the app the Critical
    Alerts entitlement; until then iOS delivers it as a normal alert.
    Repeat pages share a tag / collapse id per incident, so a re-send
    re-alerts and replaces the previous notification instead of stacking.
    Pages expire after ``PAGE_TTL_SECONDS`` undelivered, and the Android
    collapse key keeps only the newest one per incident for an offline phone.
    Unlike chat pushes, callers do not suppress pages for users active on
    the web — a page should reach every device.
    """
    tag = f"incident-{(data or {}).get('incident_id', '')}"

    def build(messaging, token_strings, payload_data):
        return messaging.MulticastMessage(
            tokens=token_strings,
            notification=messaging.Notification(title=title, body=body),
            data=payload_data,
            android=messaging.AndroidConfig(
                priority="high",
                ttl=PAGE_TTL_SECONDS,
                collapse_key=tag,
                notification=messaging.AndroidNotification(
                    channel_id=PAGE_ANDROID_CHANNEL_ID,
                    tag=tag,
                    priority="max",
                    default_sound=True,
                    default_vibrate_timings=True,
                    # The default (private) shows "Contents hidden" on lock
                    # screens that hide sensitive content.
                    visibility="public",
                ),
            ),
            apns=messaging.APNSConfig(
                headers={
                    "apns-priority": "10",
                    "apns-collapse-id": tag,
                    "apns-expiration": str(int(time.time()) + PAGE_TTL_SECONDS),
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound=messaging.CriticalSound(
                            PAGE_IOS_SOUND, critical=True, volume=1.0
                        ),
                        category=PAGE_IOS_CATEGORY,
                        thread_id=tag,
                        custom_data={"interruption-level": "critical"},
                    )
                ),
            ),
        )

    _send(user_id, data, build)


def send_page_cleared(user_id, *, alert, data=None):
    """Tell the user's devices a page was acknowledged/resolved.

    Android: data-only, so the app dismisses the page notification by tag
    (``incident-<id>``) and stops any in-app alarm. iOS: a quiet visible
    ``alert`` with the page's collapse id, so it replaces the critical alert
    in place. A background push alone is throttled and never reaches a
    force-quit app, which would leave a cleared page on the lock screen;
    content-available still hands the data to a running app. No expiration on
    purpose: a phone that got the page and then went offline needs this clear,
    however late, to replace the stale page alert.
    """
    tag = f"incident-{(data or {}).get('incident_id', '')}"

    def build(messaging, token_strings, payload_data):
        return messaging.MulticastMessage(
            tokens=token_strings,
            data=payload_data,
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                headers={
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                    "apns-collapse-id": tag,
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=alert,
                        content_available=True,
                        thread_id=tag,
                        custom_data={"interruption-level": "passive"},
                    )
                ),
            ),
        )

    _send(user_id, data, build)


def _send(user_id, data, build):
    """Deliver ``build(messaging, tokens, data)`` to every device of ``user_id``.

    Shared by every public sender: no-ops when unconfigured or tokenless,
    stamps ``last_used_at`` on success, and prunes stale tokens.
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

    message = build(messaging, token_strings, payload_data)

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
