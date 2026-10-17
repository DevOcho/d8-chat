"""
Shared helpers for e2e tests.

Selectors come from the actual templates — when a template gains a stable
``data-testid`` we should switch over, but for now we use the IDs that are
already there (``#message-form``, ``#chat-message-input``,
``#link-channel_<id>``, ``#unread-badge-<conv>``, etc.). Keep selectors in
this module so a template change is a one-line fix.

Realtime tests need to know when the server has actually *registered* a
socket or *processed* a subscription, because pub/sub is at-most-once: a
broadcast that races a subscribe is simply lost, and a test that doesn't
synchronise on it flakes. Two barriers cover that, both built on things the
app really sends (no sleeps):

* :func:`wait_for_socket` — the server broadcasts a ``presence_update`` for a
  user right after registering their socket, so seeing our *own* presence
  frame proves this page's socket is in the server's client set.
* :func:`typing_barrier` — subscribe and typing frames are processed in order
  on one socket, so when the watcher sees "<typer> is typing…", the typer's
  subscription has been processed *and* the watcher is subscribed to the same
  conversation.
"""

from __future__ import annotations

import re
import uuid
import warnings
from dataclasses import dataclass, field

from playwright.sync_api import Locator, Page, expect

# Generous because the local pod runs the Flask dev server, which pauses
# briefly whenever it hot-reloads.
REALTIME_TIMEOUT = 15_000

# Installed into every browser context (``context.add_init_script``): records
# every frame htmx's WebSocket extension delivers, so tests can wait on frames
# that have no visible DOM effect (e.g. the presence broadcast on connect).
WS_FRAME_RECORDER = """
(() => {
    if (window.__e2eFrames) return;
    window.__e2eFrames = [];
    document.addEventListener('htmx:wsAfterMessage', (event) => {
        try { window.__e2eFrames.push(String(event.detail.message)); } catch (e) {}
    });
})();
"""


@dataclass
class E2EUser:
    """A throwaway account created for this test session."""

    username: str
    password: str
    display_name: str
    user_id: int = 0
    storage_state: dict = field(default_factory=dict, repr=False)


def unique_text(label: str = "msg") -> str:
    """Message text that can't collide with earlier runs or other tests."""
    return f"e2e {label} {uuid.uuid4().hex[:12]}"


# --- Login / page readiness -------------------------------------------------


# Longest Retry-After we'll sit out on a rate-limited login (the per-minute
# window); anything longer means the hourly budget is spent, so fail instead.
MAX_LOGIN_BACKOFF_S = 65


def submit_login_form(page: Page, username: str, password: str):
    """Fill and submit the login form on ``/``; returns the POST response.

    ``POST /login`` is rate limited to 5/min and 50/hour per client IP. The
    fixtures log each account in once per session to stay under that, but
    back-to-back runs (or someone logging in by hand) can still trip the
    per-minute window. Then we wait out the server's ``Retry-After`` once —
    a server-dictated delay, not a guess — rather than fail spuriously.
    """
    for attempt in range(2):
        page.goto("/")
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        with page.expect_response(
            lambda r: (
                r.request.method == "POST" and r.url.rstrip("/").endswith("/login")
            )
        ) as info:
            page.click('button[type="submit"]:has-text("Login")')
        response = info.value
        if response.status != 429:
            return response
        retry_after = int(response.headers.get("retry-after", "60"))
        if attempt or retry_after > MAX_LOGIN_BACKOFF_S:
            break
        warnings.warn(
            f"/login rate-limited; waiting {retry_after}s (Retry-After) before retrying",
            stacklevel=2,
        )
        page.wait_for_timeout((retry_after + 1) * 1000)
    raise AssertionError(
        f"Login was rate-limited (HTTP 429, Retry-After {retry_after}s). /login "
        "allows 5/min and 50/hour per IP; wait for the window to reset and re-run."
    )


def login(page: Page, username: str, password: str) -> None:
    """Log in through the real form and wait for the chat shell."""
    submit_login_form(page, username, password)
    page.wait_for_url("**/chat", timeout=10_000)


def current_user_id(page: Page) -> int:
    """The logged-in user's id, as the chat shell renders it."""
    value = page.locator("main.main-content").get_attribute("data-current-user-id")
    return int(value)


def wait_for_socket(page: Page, user_id: int | None = None) -> None:
    """Block until the server has registered this page's WebSocket.

    Requires :data:`WS_FRAME_RECORDER` in the page's context.
    """
    if user_id is None:
        user_id = current_user_id(page)
    page.wait_for_function(
        """(uid) => (window.__e2eFrames || []).some((frame) => {
            try {
                const data = JSON.parse(frame);
                return data.type === 'presence_update' && data.user_id === uid;
            } catch (e) { return false; }
        })""",
        arg=user_id,
        timeout=REALTIME_TIMEOUT,
    )


def wait_for_composer(page: Page) -> Locator:
    """Wait for the main composer's editor to finish initializing.

    chat.js gives the send button its label and ``title`` once the editor is
    wired up (the templates render it without a title); before that a submit
    would skip the editor's handlers and post to the ``_pending_`` placeholder.
    A composer tagged by :func:`mark_view_stale` doesn't count.
    """
    button = page.locator(f"#chat-input-container:not([{STALE}]) #send-button")
    expect(button).to_have_attribute(
        "title", re.compile(r"\S"), timeout=REALTIME_TIMEOUT
    )
    return button


def wait_for_chat_ready(page: Page, user_id: int | None = None) -> None:
    """The chat shell has loaded a conversation, its composer and its socket."""
    expect(
        page.locator("#chat-messages-container > div[data-conversation-id]")
    ).to_be_attached(timeout=REALTIME_TIMEOUT)
    # On load the composer arrives twice — its own placeholder's hx-get and the
    # first conversation's OOB swap — so wait for both before using it.
    expect(page.locator("main div[hx-get='/chat/input/default']")).to_have_count(
        0, timeout=REALTIME_TIMEOUT
    )
    wait_for_composer(page)
    wait_for_socket(page, user_id)


def active_conversation_id(page: Page) -> str:
    """``conversation_id_str`` of whatever is open in the main pane."""
    return page.locator(
        "#chat-messages-container > div[data-conversation-id]"
    ).get_attribute("data-conversation-id")


# --- Sidebar ----------------------------------------------------------------


def channel_item(page: Page, name: str) -> Locator:
    """The sidebar ``<li>`` for channel ``name`` (rendered as ``# name``)."""
    label = re.compile(rf"^\s*#\s*{re.escape(name)}\s*$")
    return page.locator("#channel-list > li[id^='channel-item-']").filter(
        has=page.locator("[id^='link-channel_']", has_text=label)
    )


def channel_conv_id(page: Page, name: str) -> str:
    """``channel_<id>`` for the sidebar channel called ``name``."""
    item = channel_item(page, name)
    expect(item).to_have_count(1)
    item_id = item.get_attribute("id")  # channel-item-<id>
    return "channel_" + item_id.rsplit("-", 1)[1]


def dm_conv_id(user_id_a: int, user_id_b: int) -> str:
    low, high = sorted((user_id_a, user_id_b))
    return f"dm_{low}_{high}"


def sidebar_link(page: Page, conv_id: str) -> Locator:
    """The sidebar label for a conversation (bold when it has unreads)."""
    return page.locator(f"[id='link-{conv_id}']")


def unread_badge(page: Page, conv_id: str) -> Locator:
    """The red count pill next to a sidebar conversation."""
    return page.locator(f"[id='unread-badge-{conv_id}'] .badge")


BOLD = re.compile(r"\bfw-bold\b")


# Attribute put on the current conversation pane and composer before a
# navigation, so waits can tell the incoming ones from what's already there
# (re-opening the conversation that's open re-renders both).
STALE = "data-e2e-stale"


def mark_view_stale(page: Page) -> None:
    """Tag the open conversation pane and composer as about to be replaced."""
    page.evaluate(
        """(attr) => document.querySelectorAll(
            '#chat-messages-container > div[data-conversation-id], #chat-input-container'
        ).forEach((el) => el.setAttribute(attr, ''))""",
        STALE,
    )


def wait_for_conversation(page: Page, conv_id: str) -> None:
    """Wait for ``conv_id`` to be (re)loaded in the main pane, with a fresh,
    initialized composer. Call :func:`mark_view_stale` before navigating."""
    expect(
        page.locator(
            f"#chat-messages-container > div[data-conversation-id='{conv_id}']"
            f":not([{STALE}])"
        )
    ).to_be_attached(timeout=REALTIME_TIMEOUT)
    expect(page.locator("#message-list")).to_be_attached()
    wait_for_composer(page)


def open_channel(page: Page, name: str) -> str:
    """Open channel ``name`` from the sidebar; returns its ``channel_<id>``."""
    conv_id = channel_conv_id(page, name)
    mark_view_stale(page)
    channel_item(page, name).locator("a.sidebar-item-link").click()
    wait_for_conversation(page, conv_id)
    return conv_id


def open_general_channel(page: Page) -> str:
    """Open #general and wait for its messages to load."""
    return open_channel(page, "general")


def open_dm(page: Page, conv_id: str) -> None:
    """Open an existing DM from the sidebar."""
    mark_view_stale(page)
    page.locator(f"#dm-item-{conv_id} a.sidebar-item-link").click()
    wait_for_conversation(page, conv_id)


def open_self_dm(page: Page, user_id: int) -> str:
    """Open the user's own notes-to-self DM ("<name> (you)" in the sidebar)."""
    conv_id = dm_conv_id(user_id, user_id)
    mark_view_stale(page)
    page.locator("#dm-list a.sidebar-item-link").filter(
        has=sidebar_link(page, conv_id)
    ).click()
    wait_for_conversation(page, conv_id)
    return conv_id


# --- Composer ---------------------------------------------------------------


def composer(page: Page) -> Locator:
    """The main composer's text box, switched to the markdown textarea.

    The composer has a contenteditable WYSIWYG view and a markdown textarea;
    which one shows is a per-user preference. Tests type into the textarea, so
    flip the toggle if this user last left the rich editor on.
    """
    wait_for_composer(page)
    textarea = page.locator("#chat-input-container #markdown-toggle-view")
    if not textarea.is_visible():
        page.locator("#chat-input-container #format-toggle-btn").click()
    expect(textarea).to_be_visible()
    return textarea


def send_message(page: Page, text: str) -> None:
    """Type ``text`` into the main composer and send it.

    Web sends are an HTTP POST (the socket is push-only), so wait for that
    request to succeed; the message's appearance is asserted by the caller.
    """
    composer(page).fill(text)
    with page.expect_response(
        lambda r: (
            r.request.method == "POST"
            and "/chat/conversations/" in r.url
            and r.url.endswith("/messages")
        )
    ) as info:
        page.locator("#chat-input-container #send-button").click()
    assert info.value.ok, f"message POST failed: HTTP {info.value.status}"


def typing_barrier(typer: Page, watcher: Page, typer_username: str) -> None:
    """Prove both pages are subscribed to the conversation they have open.

    The typer types a character; the watcher must see the typing indicator.
    See the module docstring for why this is a sound barrier.
    """
    box = composer(typer)
    box.press_sequentially("…")
    expect(watcher.locator("#chat-input-container #typing-indicator")).to_contain_text(
        f"{typer_username} is typing", timeout=REALTIME_TIMEOUT
    )
    box.fill("")


# --- Messages ---------------------------------------------------------------


def message_container(page: Page, text: str, scope: str = "#message-list") -> Locator:
    """The ``.message-container`` whose body contains ``text``."""
    return page.locator(f"{scope} .message-container").filter(
        has=page.locator(".message-content", has_text=text)
    )


def wait_for_message_with_text(
    page: Page, text: str, timeout: int = REALTIME_TIMEOUT, scope: str = "#message-list"
) -> Locator:
    """Wait for a message containing ``text``; returns its content element."""
    locator = message_container(page, text, scope).locator(".message-content")
    expect(locator).to_be_visible(timeout=timeout)
    return locator


def message_id(container: Locator) -> int:
    """Numeric id of a ``.message-container`` (``id="message-<id>"``)."""
    return int(container.get_attribute("id").split("-", 1)[1])


# Message toolbar buttons, keyed by what they do. (Not by ``title``: Bootstrap
# moves the reaction button's title into ``data-bs-original-title``.)
TOOLBAR = {
    "react": "[data-bs-toggle='popover']",
    "reply": "[data-action='reply']",
    "thread": "[data-action='view-thread']",
    "edit": "[data-action='edit']",
    "delete": "[data-action='delete']",
}


def toolbar_button(container: Locator, action: str) -> Locator:
    """Hover a message so its toolbar shows; return the ``action`` button."""
    container.hover()
    button = container.locator(f".message-toolbar button{TOOLBAR[action]}")
    expect(button).to_be_visible()
    return button


def open_thread(page: Page, container: Locator) -> int:
    """Open a channel message's thread in the side panel; returns its id."""
    parent_id = message_id(container)
    toolbar_button(container, "thread").click()
    panel = page.locator("#right-panel-offcanvas")
    expect(panel).to_be_visible()
    expect(panel.locator(f"#thread-replies-list-{parent_id}")).to_be_attached()
    return parent_id


def send_thread_reply(page: Page, parent_id: int, text: str) -> None:
    """Reply in the open thread panel through its own composer."""
    suffix = f"-thread-{parent_id}"
    # Same readiness signal as the main composer: chat.js titles the button.
    button = page.locator(f"#send-button{suffix}")
    expect(button).to_have_attribute(
        "title", re.compile(r"\S"), timeout=REALTIME_TIMEOUT
    )
    textarea = page.locator(f"#markdown-toggle-view{suffix}")
    if not textarea.is_visible():
        page.locator(f"#format-toggle-btn{suffix}").click()
    textarea.fill(text)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/messages")
    ) as info:
        button.click()
    assert info.value.ok, f"thread reply POST failed: HTTP {info.value.status}"


def add_reaction(page: Page, container: Locator) -> str:
    """React to a message through its toolbar's emoji picker; returns the emoji.

    chat.js only wires the picker's ``emoji-click`` handler on Bootstrap's
    ``shown.bs.popover`` (after the fade-in), so wait for that event before
    picking, or the click is silently dropped.
    """
    page.evaluate(
        """() => {
            window.__e2ePopoverShown = false;
            document.addEventListener('shown.bs.popover',
                () => { window.__e2ePopoverShown = true; }, { once: true });
        }"""
    )
    toolbar_button(container, "react").click()
    page.wait_for_function("() => window.__e2ePopoverShown === true")
    emoji = page.locator(".popover emoji-picker").get_by_role("menuitem").first
    expect(emoji).to_be_visible(timeout=REALTIME_TIMEOUT)
    unicode = emoji.get_attribute("aria-label").split(",", 1)[0].strip()
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/react")
    ) as info:
        emoji.click()
    assert info.value.ok, f"reaction POST failed: HTTP {info.value.status}"
    return unicode


# --- Attachments ------------------------------------------------------------


def attach_file(page: Page, path: str) -> None:
    """Set the hidden file input and wait for the upload to finish."""
    wait_for_composer(page)
    page.locator("#chat-input-container #file-attachment-input").set_input_files(path)
    # The chat.js handler kicks off the upload immediately. Wait for the
    # preview thumbnail to appear, then for its spinner to clear, so we know
    # the upload finished before anyone tries to send.
    previews = page.locator("#chat-input-container #attachment-previews")
    expect(previews).to_be_visible(timeout=10_000)
    expect(previews.locator(".spinner-border")).to_have_count(0, timeout=15_000)
    expect(
        page.locator("#chat-input-container #attachment-file-ids")
    ).not_to_have_value("", timeout=15_000)
