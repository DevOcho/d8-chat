"""
Playwright fixtures and configuration.

Tests in this directory are marked ``e2e`` and excluded from the default
``pytest`` run. Opt in with::

    pytest -o addopts= -m e2e tests/e2e/

Required environment variables:

  ``E2E_BASE_URL``        — root URL of the running app (default ``https://d8-chat.local``).
  ``E2E_ADMIN_USERNAME``  — admin login (default ``admin``).
  ``E2E_ADMIN_PASSWORD``  — admin password. **Required.** Set this to whatever
                            ``INITIAL_ADMIN_PASSWORD`` was during ``init_db.py``,
                            or to the value printed/written by that script.

The Playwright browser binaries must be installed once::

    bin/python -m playwright install chromium

Logins are rationed: ``POST /login`` is rate limited to 5/min and 50/hour per
client IP, so every account logs in through the form **once per session** and
later contexts reuse its cookies (``storage_state``). Only ``test_auth.py``
drives the login form itself.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

from .helpers import (
    WS_FRAME_RECORDER,
    E2EUser,
    current_user_id,
    login,
    wait_for_chat_ready,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Satisfies the password policy (12+ chars, a letter and a number).
E2E_USER_PASSWORD = "E2e-password-12345"


def pytest_collection_modifyitems(config, items):
    """
    Auto-apply the ``e2e`` marker to every test collected from this directory.

    The global ``addopts = -m "not e2e"`` in pytest.ini excludes them by
    default; opt in with ``pytest -m e2e tests/e2e/``.
    """
    e2e_marker = pytest.mark.e2e
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(e2e_marker)


def _env(name: str, default: str | None = None) -> str:
    """Read an env var, raising if it's required and missing."""
    value = os.environ.get(name, default)
    if value is None:
        raise pytest.UsageError(
            f"Required environment variable {name!r} is not set. "
            "See tests/e2e/README.md for the full list."
        )
    return value


@pytest.fixture(scope="session")
def base_url() -> str:
    """Root URL of the running app under test."""
    return _env("E2E_BASE_URL", "https://d8-chat.local").rstrip("/")


@pytest.fixture(scope="session")
def admin_credentials() -> tuple[str, str]:
    """``(username, password)`` for an existing admin account."""
    return (
        _env("E2E_ADMIN_USERNAME", "admin"),
        _env("E2E_ADMIN_PASSWORD"),
    )


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Mute the browser: the on-call pager plays a siren, even headed."""
    args = list(browser_type_launch_args.get("args", []))
    return {**browser_type_launch_args, "args": [*args, "--mute-audio"]}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, base_url):  # noqa: PLR0913
    """
    Extend pytest-playwright's default browser context args.

    ``ignore_https_errors=True`` lets us connect to ``https://d8-chat.local``
    in a fresh browser profile that hasn't been told to trust the local
    mkcert CA. Comment this out if you want strict TLS in CI; tests against
    a real test cluster behind a real cert won't need it.
    """
    return {
        **browser_context_args,
        "ignore_https_errors": True,
        "base_url": base_url,
    }


@pytest.fixture
def fresh_context(context: BrowserContext) -> BrowserContext:
    """A browser context with no leaked state from previous tests."""
    context.clear_cookies()
    return context


@pytest.fixture
def fixture_path():
    """Builds absolute paths to ``tests/e2e/fixtures/<name>``."""
    return lambda name: str(FIXTURES_DIR / name)


# --- Accounts (one form login per account per session) ----------------------


def _login_once(browser: Browser, context_args: dict, username: str, password: str):
    """Log in through the form in a throwaway context; return its cookies and
    the user's id."""
    context = browser.new_context(**context_args)
    try:
        page = context.new_page()
        login(page, username, password)
        user_id = current_user_id(page)
        return context.storage_state(), user_id
    finally:
        context.close()


@pytest.fixture(scope="session")
def admin_storage_state(browser, browser_context_args, admin_credentials) -> dict:
    """Cookies for a logged-in admin session, shared by the whole run."""
    state, _ = _login_once(browser, browser_context_args, *admin_credentials)
    return state


@pytest.fixture(scope="session")
def admin_api(browser, browser_context_args, admin_storage_state):
    """An admin-authenticated ``APIRequestContext`` plus its CSRF token."""
    context = browser.new_context(
        **browser_context_args, storage_state=admin_storage_state
    )
    page = context.new_page()
    page.goto("/admin/")
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    page.close()
    yield context.request, token
    context.close()


@pytest.fixture(scope="session")
def create_user(
    admin_api, browser, browser_context_args, base_url
) -> Callable[[str], E2EUser]:
    """Factory: create a brand-new member through the admin UI's endpoint and
    log it in once. Fresh accounts have no unread history, no DMs, no paging
    rotation and default preferences, so tests start from a known state."""
    request, token = admin_api

    def _create(label: str) -> E2EUser:
        suffix = secrets.token_hex(4)
        user = E2EUser(
            username=f"e2e_{label}_{suffix}",
            password=E2E_USER_PASSWORD,
            display_name=f"E2E {label.title()} {suffix}",
        )
        response = request.post(
            "/admin/users/create",
            form={
                "username": user.username,
                "email": f"{user.username}@example.invalid",
                "password": user.password,
                "display_name": user.display_name,
                "role": "member",
            },
            # Flask-WTF's strict-HTTPS CSRF check also wants a same-origin Referer.
            headers={
                "X-CSRFToken": token,
                "HX-Request": "true",
                "Referer": f"{base_url}/admin/users",
            },
        )
        assert response.ok and "created successfully" in response.text(), (
            f"creating {user.username} failed: HTTP {response.status}"
        )
        user.storage_state, user.user_id = _login_once(
            browser, browser_context_args, user.username, user.password
        )
        return user

    return _create


@pytest.fixture(scope="session")
def user_a(create_user) -> E2EUser:
    """First fresh account for multi-user realtime tests."""
    return create_user("alice")


@pytest.fixture(scope="session")
def user_b(create_user) -> E2EUser:
    """Second fresh account for multi-user realtime tests."""
    return create_user("bob")


# --- Pages ------------------------------------------------------------------


@pytest.fixture
def chat_as(new_context):
    """Factory: open ``/chat`` as ``user`` in its own browser context, ready
    to use (conversation loaded, composer initialized, socket registered).

    Pass ``tab_of=<page>`` to open another tab in that page's context (same
    login, separate socket) instead, or ``on_context`` to set up a new context
    (e.g. ``route_web_socket``) before its first page loads.
    """

    def _open(
        user: E2EUser,
        *,
        tab_of: Page | None = None,
        on_context: Callable[[BrowserContext], None] | None = None,
    ) -> Page:
        if tab_of is not None:
            context = tab_of.context
        else:
            context = new_context(storage_state=user.storage_state)
            context.add_init_script(WS_FRAME_RECORDER)
            if on_context is not None:
                on_context(context)
        page = context.new_page()
        page.goto("/chat")
        wait_for_chat_ready(page, user.user_id)
        return page

    return _open


@pytest.fixture
def logged_in_page(new_context, admin_storage_state) -> Page:
    """Returns a Page on ``/chat`` that's already authenticated as the admin."""
    context = new_context(storage_state=admin_storage_state)
    context.add_init_script(WS_FRAME_RECORDER)
    page = context.new_page()
    page.goto("/chat")
    wait_for_chat_ready(page)
    return page
