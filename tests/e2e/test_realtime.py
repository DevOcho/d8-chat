"""
Realtime, multi-user behaviour — the area production bugs have come from.

Each test drives two (or three) browser contexts logged in as the session's
throwaway users ``user_a`` ("alice") and ``user_b`` ("bob") and asserts that
what one does shows up for the other *without a reload*.

Determinism: pub/sub is at-most-once, so a broadcast that races a subscribe is
lost. Before acting, tests synchronise with :func:`helpers.typing_barrier`
(see ``helpers.py``) instead of sleeping. Every message carries a uuid.
"""

import re

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from .helpers import (
    BOLD,
    REALTIME_TIMEOUT,
    E2EUser,
    add_reaction,
    composer,
    dm_conv_id,
    mark_view_stale,
    message_container,
    message_id,
    open_channel,
    open_dm,
    open_thread,
    send_message,
    send_thread_reply,
    sidebar_link,
    toolbar_button,
    typing_barrier,
    unique_text,
    unread_badge,
    wait_for_conversation,
    wait_for_message_with_text,
)


@pytest.fixture
def alice(chat_as, user_a) -> Page:
    return chat_as(user_a)


@pytest.fixture
def bob(chat_as, user_b) -> Page:
    return chat_as(user_b)


def both_in_channel(alice: Page, bob: Page, bob_user: E2EUser, name: str = "general"):
    """Open ``name`` for both users and wait until both are subscribed."""
    conv_id = open_channel(alice, name)
    open_channel(bob, name)
    typing_barrier(bob, alice, bob_user.username)
    return conv_id


def bob_away_from_general(alice: Page, bob: Page, bob_user: E2EUser) -> str:
    """Leave bob looking at #announcements with #general fully read, and
    alice in #general ready to post. Returns #general's conversation id.

    Bob reads #general first so its sidebar entry starts plain (fresh
    accounts have the whole channel history unread). The barrier in
    #announcements proves bob's socket has left #general — while it's still
    subscribed there, notifications for #general are suppressed for it.
    """
    general = open_channel(bob, "general")
    open_channel(bob, "announcements")
    open_channel(alice, "announcements")
    typing_barrier(bob, alice, bob_user.username)
    expect(sidebar_link(bob, general)).not_to_have_class(BOLD)
    expect(unread_badge(bob, general)).to_have_count(0)
    open_channel(alice, "general")
    return general


# --- Channel messages -------------------------------------------------------


def test_channel_message_appears_live_for_other_user(alice, bob, user_a, user_b):
    both_in_channel(alice, bob, user_b)

    text = unique_text("live")
    send_message(alice, text)

    wait_for_message_with_text(bob, text)
    expect(message_container(bob, text)).to_have_attribute(
        "data-user-id", str(user_a.user_id)
    )


def test_typing_indicator_clears_when_typist_leaves_channel(alice, bob, user_a, user_b):
    """The typist's client only sends typing_stop for the conversation it's
    on *now*, so leaving mid-word relies on the server's unsubscribe path to
    clear the indicator (it used to stick on forever)."""
    both_in_channel(alice, bob, user_b)
    indicator = bob.locator("#chat-input-container #typing-indicator")

    composer(alice).press_sequentially("half a thou")
    expect(indicator).to_contain_text(
        f"{user_a.username} is typing", timeout=REALTIME_TIMEOUT
    )
    open_channel(alice, "announcements")
    expect(indicator).to_have_text("", timeout=REALTIME_TIMEOUT)


# --- Unread state -----------------------------------------------------------


def test_unread_bold_appears_live_and_read_syncs_across_tabs(chat_as, user_a, user_b):
    alice = chat_as(user_a)
    bob_tab1 = chat_as(user_b)
    bob_tab2 = chat_as(user_b, tab_of=bob_tab1)  # same login, own socket

    general = bob_away_from_general(alice, bob_tab1, user_b)
    # Reading #general in tab 1 already cleared it in tab 2.
    expect(sidebar_link(bob_tab2, general)).not_to_have_class(BOLD)

    send_message(alice, unique_text("unread"))

    # Both tabs light up #general and the Unreads link, live.
    for tab in (bob_tab1, bob_tab2):
        expect(sidebar_link(tab, general)).to_have_class(BOLD, timeout=REALTIME_TIMEOUT)
    expect(bob_tab2.locator("#unreads-link")).to_have_class(BOLD)

    # Reading it in tab 1 clears it in tab 2 without a reload.
    open_channel(bob_tab1, "general")
    expect(sidebar_link(bob_tab2, general)).not_to_have_class(
        BOLD, timeout=REALTIME_TIMEOUT
    )


def test_mention_shows_badge_for_mentioned_user(alice, bob, user_b):
    general = bob_away_from_general(alice, bob, user_b)

    text = unique_text("mention")
    send_message(alice, f"{text} @{user_b.username}")

    badge = unread_badge(bob, general)
    expect(badge).to_have_text("1", timeout=REALTIME_TIMEOUT)
    expect(sidebar_link(bob, general)).to_have_class(BOLD)

    # Opening the channel clears the badge and shows the mention as a link.
    open_channel(bob, "general")
    expect(badge).to_have_count(0)
    mention = message_container(bob, text).locator(".mention-link")
    expect(mention).to_have_text(f"@{user_b.username}")


# --- Direct messages --------------------------------------------------------


def test_dm_started_from_search_appears_live_for_recipient(alice, bob, user_a, user_b):
    """Alice starts a brand-new DM from the "new message" search; bob's
    sidebar grows the conversation, badges it, and delivers live once open.

    Relies on ``user_a``/``user_b`` never having DMed — no other test opens
    this pair's DM.
    """
    conv_id = dm_conv_id(user_a.user_id, user_b.user_id)
    dm_row = bob.locator(f"#dm-item-{conv_id}")
    expect(dm_row).to_have_count(0)

    alice.get_by_title("Start a new message").click()
    modal = alice.locator("#htmx-modal")
    search = modal.get_by_placeholder("Search for people to message")
    expect(search).to_be_visible()
    search.press_sequentially(user_b.username)
    result = modal.locator(
        "#dm-user-results a.list-group-item", has_text=user_b.username
    )
    expect(result).to_have_count(1)
    mark_view_stale(alice)
    result.click()
    wait_for_conversation(alice, conv_id)

    # Opening the DM is enough for it to appear in bob's sidebar.
    expect(dm_row).to_be_visible(timeout=REALTIME_TIMEOUT)
    expect(dm_row).to_contain_text(user_a.display_name)

    first = unique_text("dm first")
    send_message(alice, first)
    expect(unread_badge(bob, conv_id)).to_have_text("1", timeout=REALTIME_TIMEOUT)
    expect(sidebar_link(bob, conv_id)).to_have_class(BOLD)

    open_dm(bob, conv_id)
    wait_for_message_with_text(bob, first)
    expect(unread_badge(bob, conv_id)).to_have_count(0)

    typing_barrier(bob, alice, user_b.username)
    second = unique_text("dm live")
    send_message(alice, second)
    wait_for_message_with_text(bob, second)


# --- Threads, reactions, edits ----------------------------------------------


def test_thread_reply_appears_live_for_other_user(alice, bob, user_b):
    both_in_channel(alice, bob, user_b)
    parent = unique_text("thread parent")
    send_message(alice, parent)
    bob_parent = message_container(bob, parent)
    expect(bob_parent).to_be_visible(timeout=REALTIME_TIMEOUT)

    parent_id = open_thread(bob, bob_parent)
    open_thread(alice, message_container(alice, parent))
    reply = unique_text("thread reply")
    send_thread_reply(alice, parent_id, reply)

    # Bob's open thread panel gets the reply, and the parent in the channel
    # grows a reply counter.
    wait_for_message_with_text(bob, reply, scope=f"#thread-replies-list-{parent_id}")
    expect(bob_parent.locator(f"#thread-indicator-{parent_id}")).to_contain_text(
        "1 reply", timeout=REALTIME_TIMEOUT
    )


def test_reaction_appears_live_for_other_user(alice, bob, user_b):
    both_in_channel(alice, bob, user_b)
    text = unique_text("react to me")
    send_message(alice, text)
    alice_msg = message_container(alice, text)
    bob_msg = message_container(bob, text)
    expect(bob_msg).to_be_visible(timeout=REALTIME_TIMEOUT)

    emoji = add_reaction(alice, alice_msg)

    bob_pill = bob_msg.locator(".reaction-pill")
    expect(bob_pill).to_have_count(1, timeout=REALTIME_TIMEOUT)
    expect(bob_pill).to_contain_text(emoji)
    expect(bob_pill.locator(".reaction-count")).to_have_text("1")

    # Bob adds the same reaction by clicking the pill; alice sees the count.
    bob_pill.click()
    expect(alice_msg.locator(".reaction-pill .reaction-count")).to_have_text(
        "2", timeout=REALTIME_TIMEOUT
    )


def test_live_reaction_highlight_is_per_viewer(alice, bob, user_b):
    """The reaction OOB fragment is rendered for whoever reacted, so each
    client must recompute its own "you reacted" highlight. (It used to arrive
    highlighted for everyone.)"""
    both_in_channel(alice, bob, user_b)
    text = unique_text("react highlight")
    send_message(alice, text)
    alice_msg = message_container(alice, text)
    bob_msg = message_container(bob, text)
    expect(bob_msg).to_be_visible(timeout=REALTIME_TIMEOUT)
    reacted = re.compile(r"\buser-reacted\b")
    alice_pill = alice_msg.locator(".reaction-pill")
    bob_pill = bob_msg.locator(".reaction-pill")

    # Alice reacts: highlighted for her, not for bob.
    add_reaction(alice, alice_msg)
    expect(bob_pill).to_have_count(1, timeout=REALTIME_TIMEOUT)
    expect(alice_pill).to_have_class(reacted)
    expect(bob_pill).not_to_have_class(reacted)

    # Bob joins in: now highlighted for both.
    bob_pill.click()
    expect(alice_pill.locator(".reaction-count")).to_have_text(
        "2", timeout=REALTIME_TIMEOUT
    )
    expect(bob_pill).to_have_class(reacted)
    expect(alice_pill).to_have_class(reacted)

    # Alice withdraws hers (a fragment rendered for a non-reactor): still
    # highlighted for bob, no longer for her.
    alice_pill.click()
    expect(bob_pill.locator(".reaction-count")).to_have_text(
        "1", timeout=REALTIME_TIMEOUT
    )
    expect(bob_pill).to_have_class(reacted)
    expect(alice_pill).not_to_have_class(reacted)


def test_edit_and_delete_propagate_live(alice, bob, user_b):
    both_in_channel(alice, bob, user_b)
    original = unique_text("before edit")
    send_message(alice, original)
    alice_msg = message_container(alice, original)
    expect(message_container(bob, original)).to_be_visible(timeout=REALTIME_TIMEOUT)
    msg_id = message_id(alice_msg)
    bob_msg = bob.locator(f"#message-{msg_id}")

    # Edit: the toolbar loads the message into the composer in edit mode.
    toolbar_button(alice_msg, "edit").click()
    expect(alice.locator("#chat-input-container .editing-block")).to_be_visible()
    edited = unique_text("after edit")
    composer(alice).fill(edited)
    with alice.expect_response(lambda r: r.request.method == "PUT") as put:
        alice.locator("#chat-input-container #send-button").click()
    assert put.value.ok, f"edit PUT failed: HTTP {put.value.status}"

    expect(bob_msg.locator(".message-content")).to_have_text(
        edited, timeout=REALTIME_TIMEOUT
    )
    expect(bob_msg).to_contain_text("(edited)")

    # Delete (after the browser's confirm dialog) removes it for bob too.
    alice.once("dialog", lambda dialog: dialog.accept())
    with alice.expect_response(lambda r: r.request.method == "DELETE") as delete:
        toolbar_button(alice.locator(f"#message-{msg_id}"), "delete").click()
    assert delete.value.ok, f"DELETE failed: HTTP {delete.value.status}"
    expect(bob_msg).to_have_count(0, timeout=REALTIME_TIMEOUT)


# --- Polls ------------------------------------------------------------------


def test_poll_appears_and_tallies_update_live(alice, bob, user_b):
    both_in_channel(alice, bob, user_b)
    question = unique_text("poll")

    # The composer's poll button opens the modal and loads the form into it.
    # (It once inherited hx-swap="none" from #message-form and spun forever.)
    alice.locator("#chat-input-container #poll-btn").click()
    expect(alice.locator("#htmx-modal")).to_be_visible()
    form = alice.locator("#create-poll-form")
    expect(form).to_be_visible(timeout=REALTIME_TIMEOUT)
    form.locator("#poll-question").fill(question)
    options = form.locator("input[name='options[]']")
    expect(options).to_have_count(2)
    options.nth(0).fill("Tacos")
    options.nth(1).fill("Pizza")
    form.get_by_role("button", name="Create Poll").click()
    expect(alice.locator("#htmx-modal")).to_be_hidden(timeout=REALTIME_TIMEOUT)

    def poll_in(page: Page):
        return page.locator("#message-list [id^='poll-']").filter(has_text=question)

    alice_poll, bob_poll = poll_in(alice), poll_in(bob)
    expect(bob_poll).to_be_visible(timeout=REALTIME_TIMEOUT)

    # Voting reveals the tallies to the voter; later votes update them live.
    alice_poll.get_by_role("button", name="Tacos").click()
    expect(alice_poll).to_contain_text(re.compile(r"\b1 votes?\b"))
    bob_poll.get_by_role("button", name="Pizza").click()
    expect(bob_poll).to_contain_text(re.compile(r"\b2 votes\b"))
    expect(alice_poll).to_contain_text(
        re.compile(r"\b2 votes\b"), timeout=REALTIME_TIMEOUT
    )


# --- Reconnect --------------------------------------------------------------


def test_messages_missed_while_disconnected_are_backfilled(chat_as, user_a, user_b):
    """Drop bob's socket so a broadcast is lost (pub/sub is at-most-once),
    then let it reconnect: the client must backfill the missed message from
    /messages/since/<id> and resume live delivery."""
    connections = []

    def proxy_socket(context: BrowserContext):
        def handle(route):
            server = route.connect_to_server()
            conn = {"route": route, "drop": False}

            def from_server(message):
                if not conn["drop"]:
                    route.send(message)

            server.on_message(from_server)
            connections.append(conn)

        context.route_web_socket(re.compile(r"/ws/chat$"), handle)

    alice = chat_as(user_a)
    bob = chat_as(user_b, on_context=proxy_socket)
    both_in_channel(alice, bob, user_b)

    # From here on bob's current socket silently loses everything.
    connections[-1]["drop"] = True
    missed = unique_text("missed")
    send_message(alice, missed)
    wait_for_message_with_text(alice, missed)
    expect(message_container(bob, missed)).to_have_count(0)

    # Kill the socket; the client reconnects on its own and backfills.
    with bob.expect_request(lambda r: "/messages/since/" in r.url):
        connections[-1]["route"].close(code=4000, reason="e2e: simulated drop")
    wait_for_message_with_text(bob, missed)
    expect(message_container(bob, missed)).to_have_count(1)
    expect(bob.locator("#connection-status-bar")).to_be_hidden(timeout=REALTIME_TIMEOUT)

    # And the new socket is subscribed again.
    typing_barrier(bob, alice, user_b.username)
    after = unique_text("after reconnect")
    send_message(alice, after)
    wait_for_message_with_text(bob, after)
