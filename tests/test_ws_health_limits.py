"""Tests for /healthz, WS event rate limiting, and the per-user socket cap."""

from unittest.mock import Mock

from app.chat_manager import MAX_SOCKETS_PER_USER, ChatManager
from app.routes import _safe_handle_frame, _ws_rate_ok


def test_healthz_ok(logged_in_client, app):
    # conftest sets chat_manager.redis_client to a Mock; ping() returns a Mock
    # (no raise), and the SQLite DB query works, so we expect a 200.
    res = logged_in_client.get("/healthz")
    assert res.status_code == 200
    data = res.get_json()
    assert data["db"] == "ok"
    assert "redis" in data


def test_healthz_reports_stale_listener(logged_in_client, app):
    import time as _time

    from app.chat_manager import chat_manager

    # A listener heartbeat older than 60s should flip the check to 503.
    chat_manager.listener_heartbeat = _time.time() - 120
    try:
        res = logged_in_client.get("/healthz")
        assert res.status_code == 503
    finally:
        chat_manager.listener_heartbeat = 0.0


def test_ws_rate_limit_blocks_flood(app):
    with app.app_context():
        ws = Mock()
        # Real numeric counters so the bucket actually depletes.
        ws._rate_tokens = 2.0
        ws._rate_last = __import__("time").time()

        allowed = sum(1 for _ in range(5) if _ws_rate_ok(ws))
        # Only ~2 tokens available and negligible refill in a tight loop.
        assert allowed <= 3


def test_ws_rate_limit_drops_frame_without_processing(app, mocker):
    with app.app_context():
        ws = Mock()
        ws._rate_tokens = 0.0
        ws._rate_last = __import__("time").time()
        proc = mocker.patch("app.routes._process_ws_event")

        _safe_handle_frame(ws, '{"type": "subscribe", "conversation_id": "x"}')

        proc.assert_not_called()  # frame dropped by the rate limiter


def test_per_user_socket_cap(mocker):
    mock_redis = Mock()
    mocker.patch("redis.from_url", return_value=mock_redis)
    mgr = ChatManager()
    mgr.redis_client = mock_redis

    sockets = [Mock() for _ in range(MAX_SOCKETS_PER_USER + 3)]
    for ws in sockets:
        mgr.set_online(1, ws)

    # Never exceeds the cap on this worker.
    assert len(mgr.all_clients[1]) <= MAX_SOCKETS_PER_USER
    # Three sockets over the cap were opened, so three were evicted+closed.
    closed = sum(1 for ws in sockets if ws.close.called)
    assert closed == 3
