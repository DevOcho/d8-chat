"""
Shared helpers for e2e tests.

Selectors come from the actual templates — when a template gains a stable
``data-testid`` we should switch over, but for now we use the IDs that are
already there (``#message-form``, ``#chat-message-input``, etc.).
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def open_general_channel(page: Page) -> None:
    """Click the #general entry in the sidebar and wait for messages to load."""
    page.locator("text=#general").first.click()
    page.wait_for_selector("#message-list", timeout=10_000)


def send_message(page: Page, text: str) -> None:
    """
    Type ``text`` into the active message input and submit.

    The default chat input has both a contenteditable WYSIWYG view and a
    markdown textarea; only one is visible at a time depending on the user's
    preference. We target the markdown textarea because tests don't depend
    on the WYSIWYG mode being on, and the value flows through to the hidden
    ``#chat-message-input`` either way.
    """
    page.fill("#markdown-toggle-view", text)
    page.click("#send-button")


def wait_for_message_with_text(page: Page, text: str, timeout: int = 10_000):
    """Wait for a message containing ``text`` to appear in the message list."""
    locator = page.locator("#message-list").locator(f'text="{text}"').first
    expect(locator).to_be_visible(timeout=timeout)
    return locator


def attach_file(page: Page, path: str) -> None:
    """Click the paperclip and set the hidden file input."""
    page.locator("#file-attachment-input").set_input_files(path)
    # The chat.js handler kicks off the upload immediately. Wait for the
    # preview thumbnail to appear so we know the upload finished before
    # someone tries to send.
    page.wait_for_selector(
        "#attachment-previews:not([style*='display: none'])", timeout=10_000
    )
    # Wait for the spinner to clear (upload completed).
    page.wait_for_selector(
        "#attachment-previews .spinner-border", state="detached", timeout=15_000
    )


def conversation_id_str_for_first_channel(page: Page) -> str:
    """
    Read the ``conversation_id_str`` of whichever channel is currently open.

    Useful when a test needs to assert against the message list for the
    channel without hard-coding the channel id.
    """
    handle = page.locator("[ws-connect]").first
    ws_url = handle.get_attribute("ws-connect")
    if not ws_url:
        return ""
    match = re.search(r"channel_(\d+)", ws_url)
    return f"channel_{match.group(1)}" if match else ""
