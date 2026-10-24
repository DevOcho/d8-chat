"""
Admin actions: create user, dashboard.

These exercise the audit-log path too — every admin mutation should write an
``AuditLog`` row, but verifying that requires DB access we don't have from
the Playwright client. Stick to UI-observable outcomes here; the audit-log
write is covered by ``tests/test_audit.py`` against the in-memory DB.
"""

import re
import secrets

import pytest
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


def _chart_drawn(page: Page) -> None:
    """The canvas shows up whether or not Chart.js ran; ask Chart.js itself."""
    expect(page.locator("#messagesChart")).to_be_visible(timeout=10_000)
    page.wait_for_function(
        "() => window.Chart && Chart.getChart('messagesChart')"
        " && Chart.getChart('messagesChart').data.labels.length === 24",
        timeout=10_000,
    )


def test_admin_dashboard_draws_the_messages_chart(logged_in_page: Page):
    """chart.js is vendored locally; the 24-hour chart must actually draw."""
    logged_in_page.goto("/admin/")
    _chart_drawn(logged_in_page)


def test_dashboard_chart_draws_when_reached_from_the_sidebar(logged_in_page: Page):
    """Regression: reaching the Dashboard from another admin page left the
    chart blank (the inline script ran before chart.js had loaded)."""
    page = logged_in_page
    page.goto("/admin/users")
    page.locator(".admin-sidebar").get_by_role("link", name="Dashboard").click()
    _chart_drawn(page)
    # The address bar and the sidebar follow the page shown.
    expect(page).to_have_url(re.compile(r"/admin/$"))
    expect(page.locator(".admin-sidebar .nav-link.active")).to_have_text(
        re.compile("Dashboard")
    )


def test_oncall_removals_ask_in_the_app_dialog(logged_in_page: Page):
    """data-confirm forms (admin pages, no inline onsubmit): Cancel keeps the
    thing, the dialog's own button goes ahead. Cleans up after itself."""
    page = logged_in_page
    page.on("dialog", lambda d: pytest.fail(f"native dialog opened: {d.message}"))
    name = f"e2e rota {secrets.token_hex(4)}"
    page.goto("/admin/oncall")
    rotations = page.locator("form[action$='/oncall/schedules']")
    rotations.locator("input[name='name']").fill(name)
    rotations.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name=name)).to_be_visible()

    page.locator("#override-start").fill("2030-01-07T09:00")
    page.locator("#override-end").fill("2030-01-08T09:00")
    page.locator("#override-reason").fill("e2e cover")
    page.get_by_role("button", name="Add override").click()
    override = page.locator("li.list-group-item", has_text="e2e cover")
    expect(override).to_be_visible()

    dialog = page.locator("#confirm-modal")
    override.get_by_role("button", name="Remove").click()
    expect(dialog.locator("#confirm-modal-body")).to_have_text("Remove this override?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(override).to_be_visible()

    override.get_by_role("button", name="Remove").click()
    dialog.locator("#confirm-modal-ok").click()
    expect(page.locator(".alert-success")).to_contain_text("Override removed.")
    expect(override).to_have_count(0)

    page.get_by_role("button", name="Delete rotation").click()
    expect(dialog.locator("#confirm-modal-ok")).to_have_text("Delete")
    dialog.locator("#confirm-modal-ok").click()
    expect(page).to_have_url(re.compile(r"/admin/oncall$"))
    expect(page.get_by_text(name)).to_have_count(0)
