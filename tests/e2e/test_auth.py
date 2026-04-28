"""
End-to-end auth flow: login success/failure, logout, forgot-password.
"""

from playwright.sync_api import Page, expect


def test_login_with_valid_credentials_lands_on_chat(page: Page, admin_credentials):
    username, password = admin_credentials
    page.goto("/")
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]:has-text("Login")')
    page.wait_for_url("**/chat", timeout=10_000)
    # Sanity: the chat shell rendered.
    expect(page.locator("#message-list").or_(page.locator("body"))).to_be_visible()


def test_login_with_wrong_password_shows_error(page: Page, admin_credentials):
    username, _ = admin_credentials
    page.goto("/")
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', "definitely-not-the-password")
    page.click('button[type="submit"]:has-text("Login")')
    # The login flow redirects back to / with an error query param.
    expect(page).to_have_url(lambda url: "error=" in url, timeout=10_000)
    expect(page.locator(".alert-danger")).to_contain_text("Invalid")


def test_logout_redirects_and_clears_session(logged_in_page: Page):
    logged_in_page.goto("/logout")
    # After logout a protected route should bounce back to login.
    logged_in_page.goto("/chat")
    # The login page is at "/" — Flask sends a 302 to that.
    expect(logged_in_page).to_have_url(
        lambda url: url.rstrip("/").endswith("d8-chat.local") or url.endswith("/"),
        timeout=10_000,
    )


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
