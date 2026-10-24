"""Run best-effort work off the request / WebSocket hot path.

Notification fan-out (dozens of queries, template renders, and synchronous FCM
HTTP) used to run inline in the sender's send path, blocking it — and under the
gevent worker, blocking DB/HTTP freezes the whole worker's event loop. Pushing
it to a background greenlet keeps the send path fast.
"""

import os
import threading

from flask import current_app

# At most this many background tasks run at once per worker; the rest wait
# for a slot. Each holds a pooled DB connection (15 per worker) while it runs,
# so one task per message, unbounded, could take the whole pool during a burst
# and leave requests waiting. (Under the gevent worker threading is patched —
# gunicorn runs without --preload — so waiting for a slot yields.)
MAX_CONCURRENT = int(os.environ.get("BACKGROUND_MAX_CONCURRENT", "5"))
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def _run_in_context(app, fn, args, kwargs):
    """Execute fn inside a fresh *request* context and clean up the DB after.

    A request context (not just an app context) is required because the
    notification fan-out builds badge/notification links with ``url_for``, and
    ``url_for`` outside a request needs ``SERVER_NAME`` (which we don't set).
    Kept as a standalone function so it can be unit-tested without spawning a
    real thread.
    """
    with _slots, app.test_request_context("/"):
        try:
            fn(*args, **kwargs)
        except Exception:  # pylint: disable=broad-exception-caught
            app.logger.exception("background task failed")
        finally:
            # pylint: disable=import-outside-toplevel
            from .models import db

            try:
                if not app.testing and not db.is_closed():
                    db.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass


def spawn_background(fn, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` off the hot path.

    Under gunicorn's gevent worker ``threading`` is monkey-patched, so this is a
    greenlet. In tests (``app.testing``) it runs synchronously so assertions stay
    deterministic and no thread escapes the test's DB/context teardown.
    """
    app = current_app._get_current_object()

    if app.testing:
        fn(*args, **kwargs)
        return

    threading.Thread(
        target=_run_in_context, args=(app, fn, args, kwargs), daemon=True
    ).start()
