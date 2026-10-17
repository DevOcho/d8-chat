"""
Admin actions: create user, dashboard.

These exercise the audit-log path too — every admin mutation should write an
``AuditLog`` row, but verifying that requires DB access we don't have from
the Playwright client. Stick to UI-observable outcomes here; the audit-log
write is covered by ``tests/test_audit.py`` against the in-memory DB.
"""

import secrets

from playwright.sync_api import Page, expect


def test_admin_creates_user_via_dashboard(logged_in_page: Page):
    page = logged_in_page
    # Random suffix so the test is repeatable on the same DB.
    suffix = secrets.token_hex(4)
    new_username = f"e2euser_{suffix}"
    new_email = f"e2euser_{suffix}@example.invalid"

    # The admin UI swaps pages into #admin-content with HTMX rather than
    # navigating, so drive it the way an admin does: Users → Create New User.
    page.goto("/admin/users")
    content = page.locator("#admin-content")
    content.get_by_role("link", name="Create New User").click()
    expect(content.get_by_role("heading", name="Create a New User")).to_be_visible()

    content.locator('input[name="username"]').fill(new_username)
    content.locator('input[name="email"]').fill(new_email)
    content.locator('input[name="password"]').fill("TempPassword12345!")
    content.locator('input[name="display_name"]').fill(f"E2E User {suffix}")
    content.get_by_role("button", name="Create User").click()

    # The response swaps the user list back in, with a success flash and the
    # new user in the table.
    expect(content.locator(".alert-success")).to_contain_text(
        f"User '{new_username}' created successfully", timeout=10_000
    )
    expect(content.get_by_role("cell", name=new_username, exact=True)).to_be_visible()


def test_admin_dashboard_loads(logged_in_page: Page):
    """Sanity: chart.js is vendored locally — confirm the dashboard loads
    without going to the network for chart.js."""
    logged_in_page.goto("/admin/")
    # The dashboard partial includes a `<canvas id="messagesChart">`.
    expect(logged_in_page.locator("#messagesChart")).to_be_visible(timeout=10_000)
