"""
Send messages and exercise rendering paths the audit flagged as fragile:
fenced code blocks (which bypass bleach via the placeholder substitution
trick), inline emphasis, and bare URLs (linkify).
"""

from playwright.sync_api import Page, expect

from .helpers import open_general_channel, send_message, wait_for_message_with_text


def test_send_simple_message_appears_in_message_list(logged_in_page: Page):
    open_general_channel(logged_in_page)
    text = "e2e: hello from playwright"
    send_message(logged_in_page, text)
    locator = wait_for_message_with_text(logged_in_page, text)
    expect(locator).to_be_visible()


def test_fenced_code_block_renders_as_pre_code(logged_in_page: Page):
    """
    The audit specifically called out the code-fence pipeline because it
    bypasses bleach. This test asserts the *rendered* output is a `<pre><code>`
    element with the literal source preserved (no parsing of inner HTML).
    """
    open_general_channel(logged_in_page)
    fenced = "```\n<script>alert('x')</script>\n```"
    send_message(logged_in_page, fenced)

    # Wait for the pre/code block to appear.
    pre = logged_in_page.locator("#message-list pre code").last
    expect(pre).to_be_visible(timeout=10_000)
    # The literal `<script>` text should be inside the code element as text,
    # not as a real element. innerText preserves it; querying for a child
    # script tag would prove it didn't render.
    expect(pre).to_contain_text("<script>alert('x')</script>")
    # Defensive: there must not be a live <script> inside the message list.
    assert logged_in_page.locator("#message-list script").count() == 0


def test_inline_code_renders_as_code_element(logged_in_page: Page):
    open_general_channel(logged_in_page)
    send_message(logged_in_page, "use `print('hi')` to debug")
    code = logged_in_page.locator("#message-list code:has-text(\"print('hi')\")").last
    expect(code).to_be_visible(timeout=10_000)


def test_url_in_message_is_linkified(logged_in_page: Page):
    open_general_channel(logged_in_page)
    send_message(logged_in_page, "see https://example.com for details")
    link = logged_in_page.locator('#message-list a[href="https://example.com"]').last
    expect(link).to_be_visible(timeout=10_000)
    # Sanitization callback adds these.
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
