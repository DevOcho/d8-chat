"""
Send messages and exercise rendering paths the audit flagged as fragile:
fenced code blocks (which bypass bleach via the placeholder substitution
trick), inline emphasis, and bare URLs (linkify).

Every message carries a unique token so a rerun can't pass by matching an
older message with the same text.
"""

import uuid

from playwright.sync_api import Page, expect

from .helpers import (
    message_container,
    open_general_channel,
    send_message,
    unique_text,
    wait_for_message_with_text,
)


def test_send_simple_message_appears_in_message_list(logged_in_page: Page):
    open_general_channel(logged_in_page)
    text = unique_text("hello from playwright")
    send_message(logged_in_page, text)
    locator = wait_for_message_with_text(logged_in_page, text)
    expect(locator).to_have_text(text)


def test_fenced_code_block_renders_as_pre_code(logged_in_page: Page):
    """
    The audit specifically called out the code-fence pipeline because it
    bypasses bleach. This test asserts the *rendered* output is a `<pre><code>`
    element with the literal source preserved (no parsing of inner HTML).
    """
    open_general_channel(logged_in_page)
    token = uuid.uuid4().hex[:12]
    script = f"<script>alert('{token}')</script>"
    send_message(logged_in_page, f"```\n{script}\n```")

    # Wait for the pre/code block to appear.
    pre = logged_in_page.locator("#message-list pre code").filter(has_text=token)
    expect(pre).to_be_visible(timeout=10_000)
    # The literal `<script>` text should be inside the code element as text,
    # not as a real element. innerText preserves it; querying for a child
    # script tag would prove it didn't render.
    expect(pre).to_contain_text(script)
    # Defensive: there must not be a live <script> inside the message list.
    expect(logged_in_page.locator("#message-list script")).to_have_count(0)


def test_inline_code_renders_as_code_element(logged_in_page: Page):
    open_general_channel(logged_in_page)
    token = uuid.uuid4().hex[:12]
    send_message(logged_in_page, f"use `print('{token}')` to debug")
    code = logged_in_page.locator("#message-list code").filter(
        has_text=f"print('{token}')"
    )
    expect(code).to_be_visible(timeout=10_000)


def test_url_in_message_is_linkified(logged_in_page: Page):
    open_general_channel(logged_in_page)
    text = unique_text("see https://example.com for details")
    send_message(logged_in_page, text)
    message = message_container(logged_in_page, text)
    link = message.locator('a[href="https://example.com"]')
    expect(link).to_be_visible(timeout=10_000)
    # Sanitization callback adds these.
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
