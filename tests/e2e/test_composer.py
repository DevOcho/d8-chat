"""
The composer's send key and lists, in a real browser.

- The send-key preference applies as soon as it's changed in Preferences,
  without a reload.
- Inside a list, the new-line key starts the next item (Shift+Enter when Enter
  sends), and on an empty item it ends the list. In the rich editor that relies
  on the browser's own editing command, which the jsdom unit tests can't run.

Runs as ``user_b``. The shared ``send_message`` helper clicks the Send button,
so the send-key preference doesn't affect other tests; these tests still put
it back to "Enter sends" when they finish.
"""

import pytest
from playwright.sync_api import Page, expect

from .helpers import (
    REALTIME_TIMEOUT,
    composer,
    open_general_channel,
    unique_text,
    wait_for_composer,
    wait_for_message_with_text,
)


def is_message_post(response) -> bool:
    return (
        response.request.method == "POST"
        and "/chat/conversations/" in response.url
        and response.url.endswith("/messages")
    )


def choose_send_key(page: Page, enter_sends: bool) -> None:
    """Change the preference the way a person does: Profile -> Preferences."""
    page.locator("#sidebar-profile-button").click()
    profile = page.locator("#right-panel-offcanvas")
    profile.get_by_role("button", name="Preferences").click()
    select = profile.locator("#send-key-select")
    expect(select).to_be_visible()
    with page.expect_response(
        lambda r: r.url.endswith("/profile/send_on_enter")
    ) as info:
        select.select_option("true" if enter_sends else "false")
    assert info.value.ok, f"send_on_enter returned HTTP {info.value.status}"
    profile.get_by_role("button", name="Close").click()
    expect(profile).to_be_hidden()
    expect(page.locator("main.main-content")).to_have_attribute(
        "data-send-on-enter", "true" if enter_sends else "false"
    )


def rich_editor(page: Page):
    """The main composer's rich editor (flipping the toggle if needed)."""
    wait_for_composer(page)
    editor = page.locator("#chat-input-container #wysiwyg-editor")
    if not editor.is_visible():
        page.locator("#chat-input-container #format-toggle-btn").click()
    expect(editor).to_be_visible()
    return editor


@pytest.fixture
def chat_page(chat_as, user_b):
    page = chat_as(user_b)
    open_general_channel(page)
    # A reload would reset this marker; the preference must apply without one.
    page.evaluate("window.__noReload = true")
    yield page
    if not page.is_closed():
        choose_send_key(page, enter_sends=True)


def test_send_key_change_applies_without_reload(chat_page: Page):
    page = chat_page
    posts = []
    page.on(
        "request",
        lambda r: (
            posts.append(r.url)
            if r.method == "POST" and r.url.endswith("/messages")
            else None
        ),
    )

    choose_send_key(page, enter_sends=False)
    expect(page.locator("#chat-input-container #send-button")).to_have_attribute(
        "title", "Send (Ctrl+Enter)"
    )
    box = composer(page)
    first = unique_text("ctrl-enter")
    box.fill(first)
    box.press("Enter")
    expect(box).to_have_value(f"{first}\n")
    assert posts == [], "Enter sent the message after switching to Ctrl+Enter"
    with page.expect_response(is_message_post) as info:
        box.press("Control+Enter")
    assert info.value.ok
    wait_for_message_with_text(page, first)

    choose_send_key(page, enter_sends=True)
    second = unique_text("enter-again")
    box.fill(second)
    with page.expect_response(is_message_post) as info:
        box.press("Enter")
    assert info.value.ok
    wait_for_message_with_text(page, second)
    assert page.evaluate("window.__noReload") is True


def test_shift_enter_builds_a_bullet_list_in_the_rich_editor(chat_page: Page):
    page = chat_page
    choose_send_key(page, enter_sends=True)
    editor = rich_editor(page)
    one, two, after = unique_text("one"), unique_text("two"), unique_text("after list")

    editor.click()
    page.locator("#chat-input-container [data-command='insertUnorderedList']").click()
    page.keyboard.type(one)
    page.keyboard.press("Shift+Enter")
    page.keyboard.type(two)
    # Shift+Enter on the new, empty bullet ends the list.
    page.keyboard.press("Shift+Enter")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type(after)

    items = editor.locator("ul > li")
    expect(items).to_have_text([one, two])
    expect(editor.locator("ul")).not_to_contain_text(after)
    expect(editor).to_contain_text(after)

    with page.expect_response(is_message_post) as info:
        page.keyboard.press("Enter")
    assert info.value.ok
    message = wait_for_message_with_text(page, after)
    expect(message.locator("ul > li")).to_have_text(
        [one, two], timeout=REALTIME_TIMEOUT
    )
    expect(message.locator("ul")).not_to_contain_text(after)


def test_shift_enter_continues_a_list_in_the_markdown_view(chat_page: Page):
    page = chat_page
    choose_send_key(page, enter_sends=True)
    box = composer(page)
    one, two = unique_text("md one"), unique_text("md two")

    box.fill(f"- {one}")
    box.press("End")
    box.press("Shift+Enter")
    page.keyboard.type(two)
    expect(box).to_have_value(f"- {one}\n- {two}")

    with page.expect_response(is_message_post) as info:
        box.press("Enter")
    assert info.value.ok
    message = wait_for_message_with_text(page, two)
    expect(message.locator("ul > li")).to_have_text(
        [one, two], timeout=REALTIME_TIMEOUT
    )
