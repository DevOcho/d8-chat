"""Helpers for hardening flask-sock / simple_websocket connections."""

import socket
import threading

# Concrete lock types, used to tell a real installed lock apart from a test
# Mock's auto-created attribute when deciding whether to serialize a send.
LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))


class LockedSocket:
    """Serialize ``sock.send`` so application data frames can't interleave with
    simple_websocket's own PING control frames.

    simple_websocket's ``Base._thread`` writes ``Ping()`` frames directly via
    ``self.sock.send`` from a background thread, while the listener and
    notification paths write data frames via ``ws.send`` → ``self.sock.send``.
    Under gevent a large send can yield mid-write, so without a shared lock two
    writers can interleave bytes on the wire and corrupt the WebSocket framing
    (seen as spurious 1006 drops under load). Every non-send attribute
    (``recv``, ``fileno``, ``settimeout``, ``close`` …) delegates to the real
    socket.
    """

    def __init__(self, sock, lock):
        self._sock = sock
        self._lock = lock

    def send(self, data):
        with self._lock:
            return self._sock.send(data)

    def __getattr__(self, name):
        # Only reached for attributes not set on the wrapper itself.
        return getattr(self._sock, name)


def harden_ws(ws):
    """Attach a shared reentrant send lock, wrap the raw socket, and set a send
    timeout. Call once per connection right after the upgrade completes.

    Returns the lock so callers/tests can reference it. The lock is reentrant
    (``RLock``) so ``ChatManager._send_message`` can hold it around the whole
    ``ws.send`` — which internally re-enters via ``LockedSocket.send`` on the
    same thread — without self-deadlock.
    """
    lock = threading.RLock()
    ws._d8_send_lock = lock
    try:
        ws.sock = LockedSocket(ws.sock, lock)
        # A wedged client (send buffer full, never draining) must raise instead
        # of blocking the listener/ping thread forever. The receive path stays
        # correct because simple_websocket selects for readability before each
        # recv when ping_interval is set.
        ws.sock.settimeout(30)
    except Exception:  # pragma: no cover - defensive; socket shape may vary
        pass
    return lock


def reject_ws(ws, message):
    """Close a just-upgraded socket with 1008 (policy violation).

    Under the Werkzeug dev server, returning from the handler makes Werkzeug
    write an HTTP ``200`` response onto the upgraded socket right after our
    close frame. Browsers parse those bytes as a corrupt frame and report 1006
    instead of 1008, so the client treats the reject as a network drop and
    reconnects forever. Shutting down the write side makes that trailing write
    fail as a dropped connection, which Werkzeug ignores. gunicorn (production)
    never writes the response, so this is a no-op there.
    """
    ws.close(reason=1008, message=message)
    if getattr(ws, "mode", None) == "werkzeug":
        try:
            ws.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
