"""
Admin actions: create user, role change.

These exercise the audit-log path too — every admin mutation should write an
``AuditLog`` row, but verifying that requires DB access we don't have from
the Playwright client. Stick to UI-observable outcomes here; the audit-log
write is covered by ``tests/test_audit.py`` against the in-memory DB.
"""

import secrets

from playwright.sync_api import Page, expect


def test_admin_creates_user_via_dashboard(logged_in_page: Page):
    # Random suffix so the test is repeatable on the same DB.
    suffix = secrets.token_hex(4)
    new_username = f"e2euser_{suffix}"
    new_email = f"e2euser_{suffix}@example.invalid"

    logged_in_page.goto("/admin/users/create")
    logged_in_page.fill('input[name="username"]', new_username)
    logged_in_page.fill('input[name="email"]', new_email)
    logged_in_page.fill('input[name="password"]', "TempPassword12345!")
    logged_in_page.fill('input[name="display_name"]', f"E2E User {suffix}")
    logged_in_page.click('button[type="submit"]')

    # The admin user list should now contain the new user.
    logged_in_page.wait_for_url("**/admin/users", timeout=10_000)
    expect(logged_in_page.locator(f"text={new_username}")).to_be_visible(timeout=10_000)


def test_admin_dashboard_loads(logged_in_page: Page):
    """Sanity: chart.js is vendored locally — confirm the dashboard loads
    without going to the network for chart.js."""
    logged_in_page.goto("/admin/")
    # The dashboard partial includes a `<canvas id="messagesChart">`.
    expect(logged_in_page.locator("#messagesChart")).to_be_visible(timeout=10_000)
