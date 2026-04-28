"""
Playwright fixtures and configuration.

Tests in this directory are marked ``e2e`` and excluded from the default
``pytest`` run. Opt in with::

    pytest -m e2e tests/e2e/

Required environment variables:

  ``E2E_BASE_URL``        — root URL of the running app (default ``https://d8-chat.local``).
  ``E2E_ADMIN_USERNAME``  — admin login (default ``admin``).
  ``E2E_ADMIN_PASSWORD``  — admin password. **Required.** Set this to whatever
                            ``INITIAL_ADMIN_PASSWORD`` was during ``init_db.py``,
                            or to the value printed/written by that script.

The Playwright browser binaries must be installed once::

    bin/python -m playwright install chromium
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import BrowserContext, Page

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
def fresh_context(context: BrowserContext) -> Iterator[BrowserContext]:
    """A browser context with no leaked state from previous tests."""
    context.clear_cookies()
    yield context


@pytest.fixture
def fixture_path():
    """Builds absolute paths to ``tests/e2e/fixtures/<name>``."""
    return lambda name: str(FIXTURES_DIR / name)


# --- Login helpers ----------------------------------------------------------


def _login(page: Page, username: str, password: str) -> None:
    """Submit the login form on ``/`` and wait for the redirect to ``/chat``."""
    page.goto("/")
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]:has-text("Login")')
    page.wait_for_url("**/chat", timeout=10_000)


@pytest.fixture
def logged_in_page(page: Page, admin_credentials) -> Page:
    """Returns a Page that's already authenticated as the admin user."""
    username, password = admin_credentials
    _login(page, username, password)
    return page
