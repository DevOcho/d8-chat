"""
On-call pager (``static/js/pager.js``): the profile's "Test my pager" button
rings the user's own pager over the WebSocket, and stopping it clears the
full-screen alert.

Runs as a fresh session user rather than the seeded on-call ``engineer``: a
test page works for anyone, and a fresh account can't have a real incident
already ringing — whose overlay would sit on top of the whole UI. The browser
is launched with ``--mute-audio`` (see conftest); nothing here depends on the
siren actually playing.
"""

import re

from playwright.sync_api import expect

from .helpers import REALTIME_TIMEOUT

PAGER_ACTIVE = re.compile(r"\bpager-active\b")


def test_test_page_rings_pager_and_stop_clears_it(chat_as, user_a):
    page = chat_as(user_a)
    overlay = page.locator("#pager-overlay")
    expect(overlay).to_be_hidden()

    page.locator("#sidebar-profile-button").click()
    profile = page.locator("#right-panel-offcanvas")
    profile.get_by_role("button", name="Notifications").click()
    with page.expect_response(lambda r: r.url.endswith("/oncall/test-page")) as info:
        profile.get_by_role("button", name="Test my pager").click()
    assert info.value.status == 204, f"test-page returned HTTP {info.value.status}"

    # The page arrives as a WebSocket frame and raises the alert.
    expect(overlay).to_be_visible(timeout=REALTIME_TIMEOUT)
    incident = overlay.locator(".pager-incident")
    expect(incident).to_have_count(1)
    expect(incident.locator(".pager-incident-title")).to_contain_text("Test page")
    expect(page.locator("body")).to_have_class(PAGER_ACTIVE)

    incident.get_by_role("button", name="Stop test").click()
    expect(overlay).to_be_hidden()
    expect(page.locator("body")).not_to_have_class(PAGER_ACTIVE)
