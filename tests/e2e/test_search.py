"""
Global search: finding a message and jumping to it, and never surfacing
another user's private conversation.
"""

from playwright.sync_api import Page, expect

from .helpers import (
    REALTIME_TIMEOUT,
    message_container,
    open_channel,
    open_self_dm,
    send_message,
    unique_text,
    wait_for_message_with_text,
)


def search(page: Page, query: str):
    """Run a full search (Enter) and return the results overlay."""
    box = page.locator("#global-search-input")
    box.fill(query)
    box.press("Enter")
    overlay = page.locator("#search-results-overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator("#search-results-header")).to_contain_text(
        query, timeout=REALTIME_TIMEOUT
    )
    return overlay


def test_search_finds_message_and_jumps_to_it(chat_as, user_a):
    page = chat_as(user_a)
    general = open_channel(page, "general")
    text = unique_text("searchable")
    send_message(page, text)
    wait_for_message_with_text(page, text)
    open_channel(page, "announcements")

    overlay = search(page, text)
    result = overlay.locator(".search-result-item").filter(has_text=text)
    expect(result).to_have_count(1)
    result.click()

    # The result opens the message in its channel and closes the overlay.
    expect(overlay).to_be_hidden()
    expect(
        page.locator(
            f"#chat-messages-container > div[data-conversation-id='{general}']"
        )
    ).to_be_attached(timeout=REALTIME_TIMEOUT)
    expect(message_container(page, text)).to_be_visible(timeout=REALTIME_TIMEOUT)


def test_search_does_not_reveal_other_users_private_messages(chat_as, user_a, user_b):
    alice = chat_as(user_a)
    open_self_dm(alice, user_a.user_id)
    text = unique_text("alice private note")
    send_message(alice, text)
    wait_for_message_with_text(alice, text)

    # Alice can find it...
    expect(
        search(alice, text).locator(".search-result-item").filter(has_text=text)
    ).to_have_count(1)

    # ...bob can't.
    bob = chat_as(user_b)
    overlay = search(bob, text)
    expect(overlay.locator(".search-result-item")).to_have_count(0)
    expect(overlay.locator("#messages-tab-pane")).to_contain_text("No messages found")
