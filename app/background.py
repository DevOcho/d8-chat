"""Run best-effort work off the request / WebSocket hot path.

Notification fan-out (dozens of queries, template renders, and synchronous FCM
HTTP) used to run inline in the sender's send path, blocking it — and under the
gevent worker, blocking DB/HTTP freezes the whole worker's event loop. Pushing
it to a background greenlet keeps the send path fast.
"""

import threading

from flask import current_app


def spawn_background(fn, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` off the hot path.

    Under gunicorn's gevent worker ``threading`` is monkey-patched, so this is a
    greenlet. In tests (``app.testing``) it runs synchronously so assertions stay
    deterministic and no thread escapes the test's DB/context teardown. The
    background run gets its own app context and returns its DB connection to the
    pool when done.
    """
    app = current_app._get_current_object()

    if app.testing:
        fn(*args, **kwargs)
        return

    def _run():
        with app.app_context():
            try:
                fn(*args, **kwargs)
            except Exception:  # pylint: disable=broad-exception-caught
                app.logger.exception("background task failed")
            finally:
                # pylint: disable=import-outside-toplevel
                from .models import db

                try:
                    if not db.is_closed():
                        db.close()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

    threading.Thread(target=_run, daemon=True).start()
