"""Tests for the off-hot-path background runner.

Regression guard: notification fan-out moved to a background thread must still
run inside a *request* context, or url_for() (used to build badge/notification
links) raises 'Unable to build URLs outside an active request'. The synchronous
testing shortcut in spawn_background masked this, so we test the real context
runner (_run_in_context) directly.
"""

from flask import has_request_context, url_for

from app.background import _run_in_context, spawn_background


def test_run_in_context_provides_request_context(app):
    seen = {}

    def task():
        seen["has_request_context"] = has_request_context()
        # This is exactly what the notification fan-out does and what blew up
        # when only an app context was present.
        seen["url"] = url_for("static", filename="favicon.ico")

    _run_in_context(app, task, (), {})

    assert seen["has_request_context"] is True
    assert seen["url"].endswith("favicon.ico")


def test_run_in_context_swallows_exceptions(app):
    # A failing task must not propagate (it's best-effort, off the hot path).
    def boom():
        raise RuntimeError("kaboom")

    _run_in_context(app, boom, (), {})  # must not raise


def test_spawn_background_runs_synchronously_under_testing(app):
    calls = []
    with app.test_request_context("/"):
        spawn_background(lambda: calls.append(1))
    assert calls == [1]
