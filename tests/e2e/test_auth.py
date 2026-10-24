"""
End-to-end auth flow: login success/failure, logout, forgot-password.

These are the only tests that drive the login form directly; everything else
reuses a session from ``conftest.py`` because ``POST /login`` is rate limited.
"""

import re

from playwright.sync_api import Page, expect

from .helpers import submit_login_form


def _login_page_url(base_url: str) -> re.Pattern:
    """The login page: the site root, optionally with a query string."""
    return re.compile(rf"^{re.escape(base_url)}/?(\?.*)?$")


def test_login_with_valid_credentials_lands_on_chat(page: Page, admin_credentials):
    username, password = admin_credentials
    submit_login_form(page, username, password)
    page.wait_for_url("**/chat", timeout=10_000)
    # Sanity: the chat shell rendered and loaded a conversation.
    expect(page.locator("main.main-content")).to_be_visible()
    expect(
        page.locator("#chat-messages-container > div[data-conversation-id]")
    ).to_be_attached(timeout=10_000)


def test_login_with_wrong_password_shows_error(page: Page, admin_credentials):
    username, _ = admin_credentials
    submit_login_form(page, username, "definitely-not-the-password")
    # The login flow redirects back to / with an error query param.
    expect(page).to_have_url(re.compile(r"[?&]error="), timeout=10_000)
    expect(page.locator(".alert-danger")).to_contain_text("Invalid")


def test_logout_redirects_and_clears_session(logged_in_page: Page, base_url: str):
    # /logout is POST-only — drive it through the profile UI's submit button
    # so the CSRF token is included automatically.
    logged_in_page.locator("#sidebar-profile-button").click()
    logged_in_page.get_by_role("button", name="Logout").click()
    expect(logged_in_page).to_have_url(_login_page_url(base_url), timeout=10_000)
    # After logout a protected route should bounce back to the login page.
    logged_in_page.goto("/chat")
    expect(logged_in_page).to_have_url(_login_page_url(base_url), timeout=10_000)
    expect(logged_in_page.locator('input[name="password"]')).to_be_visible()


def test_forgot_password_form_accepts_email(page: Page):
    page.goto("/forgot-password")
    page.fill('input[name="email"]', "definitely-nobody@example.invalid")
    page.click('button[type="submit"]:has-text("Send reset link")')
    # The response is the same whether the email exists or not — tests our
    # account-enumeration protection.
    expect(page.locator(".alert-info")).to_contain_text("If that email exists")


def test_forgot_password_link_visible_on_login_page(page: Page):
    page.goto("/")
    expect(page.locator('a:has-text("Forgot password?")')).to_be_visible()


def test_password_visibility_toggle(page: Page):
    page.goto("/")
    password = page.locator("#password")
    toggle = page.locator("#toggle-password-visibility")
    expect(password).to_have_attribute("type", "password")
    toggle.click()
    expect(password).to_have_attribute("type", "text")
    expect(toggle.locator("i")).to_have_class(re.compile(r"\bbi-eye\b"))
    toggle.click()
    expect(password).to_have_attribute("type", "password")
    expect(toggle.locator("i")).to_have_class(re.compile(r"\bbi-eye-slash\b"))
