"""
Presence: the web client's activity heartbeat (``POST /profile/activity``,
sent by the PresenceManager in chat.js on load and while the user is active).

The server only shows a user's dot as "online" when they're both connected
*and* recently active, and it uses the same activity stamp to suppress mobile
pushes while someone is at the web app — so if this request fails, presence
and push gating are silently wrong. (It did: the ping was a bare ``fetch()``
without the CSRF header, so every one was rejected with a 400.)
"""

from .helpers import WS_FRAME_RECORDER, wait_for_chat_ready


def test_activity_heartbeat_is_accepted(new_context, user_a):
    context = new_context(storage_state=user_a.storage_state)
    context.add_init_script(WS_FRAME_RECORDER)
    page = context.new_page()
    # The first heartbeat is sent as soon as the chat shell loads.
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/profile/activity")
    ) as info:
        page.goto("/chat")
    wait_for_chat_ready(page, user_a.user_id)
    assert info.value.status == 204, (
        f"POST /profile/activity returned HTTP {info.value.status}: "
        f"{info.value.text()[:200]!r}"
    )
