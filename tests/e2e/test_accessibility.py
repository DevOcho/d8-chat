"""
Accessibility e2e: axe-core (WCAG 2.0/2.1, levels A and AA) on the main screens.

axe-core is a dev dependency (``npm install``); its bundle is injected into
each page and run against the whole document, or against the region a test
opened (a modal, the right panel, the quick switcher). Any violation fails
the test with axe's rule id, the offending elements and how to fix them.

Not covered by axe (manual checks): reading order and announcements with a
real screen reader, keyboard traps inside third-party widgets (the emoji
picker), and anything only reachable after actions these tests don't take.
Dark mode is checked by switching the theme attribute in place, which is
what the theme setting does on load.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .helpers import REALTIME_TIMEOUT, open_general_channel, wait_for_chat_ready

AXE_BUNDLE = (
    Path(__file__).resolve().parents[2] / "node_modules" / "axe-core" / "axe.min.js"
)
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

pytestmark = pytest.mark.skipif(
    not AXE_BUNDLE.exists(), reason="axe-core isn't installed: run `npm install`"
)


def axe_violations(page: Page, context: str | None = None) -> list[dict]:
    """Run axe on the page (or the element matching ``context``)."""
    # A message mid fade-in (.just-arrived) or still in flight (.pending-message,
    # dimmed) is partly transparent, so axe would measure a contrast it never
    # settles at. Scan once the timeline is at rest.
    page.wait_for_function(
        "!document.querySelector('.just-arrived, .pending-message')", timeout=5000
    )
    # Evaluated over DevTools rather than injected as a <script>: the page's
    # CSP (script-src 'self') would block an injected inline script.
    if not page.evaluate("typeof window.axe === 'object'"):
        page.evaluate(AXE_BUNDLE.read_text(encoding="utf-8") + "\n;0")
    return page.evaluate(
        """async ([selector, tags]) => {
            const root = selector ? document.querySelector(selector) : document;
            const result = await axe.run(root, { runOnly: { type: 'tag', values: tags } });
            return result.violations.map((v) => ({
                id: v.id,
                impact: v.impact,
                help: v.help,
                url: v.helpUrl,
                nodes: v.nodes.map((n) => ({ target: n.target.join(' '), summary: n.failureSummary })),
            }));
        }""",
        [context, WCAG_TAGS],
    )


def assert_accessible(page: Page, where: str, context: str | None = None) -> None:
    violations = axe_violations(page, context)
    if not violations:
        return
    lines = [f"{len(violations)} accessibility violation(s) on {where}:"]
    for v in violations:
        lines.append(f"  [{v['impact']}] {v['id']}: {v['help']} ({v['url']})")
        for node in v["nodes"][:5]:
            lines.append(f"      {node['target']}")
            for detail in (node["summary"] or "").splitlines()[1:3]:
                lines.append(f"        {detail.strip()}")
    pytest.fail("\n".join(lines), pytrace=False)


def test_login_page_is_accessible(page: Page, base_url):
    page.goto(base_url + "/")
    expect(page.locator("form[action='/login']")).to_be_visible()
    assert_accessible(page, "the login page")


def test_forgot_password_page_is_accessible(page: Page, base_url):
    page.goto(base_url + "/forgot-password")
    assert_accessible(page, "the forgot-password page")


def test_chat_is_accessible(logged_in_page: Page):
    open_general_channel(logged_in_page)
    assert_accessible(logged_in_page, "#general")


def test_chat_is_accessible_in_dark_mode(logged_in_page: Page):
    open_general_channel(logged_in_page)
    logged_in_page.evaluate(
        "document.documentElement.setAttribute('data-bs-theme', 'dark')"
    )
    assert_accessible(logged_in_page, "#general in dark mode")


def test_quick_switcher_is_accessible(logged_in_page: Page):
    open_general_channel(logged_in_page)
    logged_in_page.keyboard.press("Control+k")
    expect(logged_in_page.locator("#quick-switcher")).to_be_visible()
    assert_accessible(logged_in_page, "the quick switcher", "#quick-switcher")


def test_start_dm_modal_is_accessible(logged_in_page: Page):
    open_general_channel(logged_in_page)
    logged_in_page.locator("button[title='Start a new message']").click()
    expect(logged_in_page.locator("#htmx-modal.show")).to_be_visible(
        timeout=REALTIME_TIMEOUT
    )
    expect(logged_in_page.locator("#htmx-modal input.form-control")).to_be_visible(
        timeout=REALTIME_TIMEOUT
    )
    assert_accessible(logged_in_page, "the new-message modal", "#htmx-modal")


def test_profile_panel_is_accessible(logged_in_page: Page):
    open_general_channel(logged_in_page)
    logged_in_page.locator("#sidebar-profile-button").click()
    expect(logged_in_page.locator("#profile-header-card")).to_be_visible(
        timeout=REALTIME_TIMEOUT
    )
    assert_accessible(logged_in_page, "the profile panel", "#right-panel-offcanvas")


def test_unreads_and_threads_views_are_accessible(logged_in_page: Page):
    open_general_channel(logged_in_page)
    logged_in_page.locator("#unreads-link").click()
    expect(
        logged_in_page.locator("#chat-messages-container > div[data-conversation-id]")
    ).to_have_count(0, timeout=REALTIME_TIMEOUT)
    assert_accessible(logged_in_page, "Unreads")
    logged_in_page.locator("#threads-link").click()
    expect(logged_in_page.locator("#threads-view")).to_be_attached(
        timeout=REALTIME_TIMEOUT
    )
    assert_accessible(logged_in_page, "Threads")


def test_search_results_are_accessible(logged_in_page: Page):
    wait_for_chat_ready(logged_in_page)
    search = logged_in_page.locator("#global-search-input")
    search.fill("e2e")
    search.press("Enter")
    expect(logged_in_page.locator("#search-results-overlay .nav-tabs")).to_be_visible(
        timeout=REALTIME_TIMEOUT
    )
    assert_accessible(logged_in_page, "search results", "#search-results-overlay")


@pytest.mark.parametrize(
    "path, where",
    [
        ("/admin/", "the admin dashboard"),
        ("/admin/users", "admin users"),
        ("/admin/channels", "admin channels"),
        ("/admin/oncall", "admin on-call"),
    ],
)
def test_admin_pages_are_accessible(logged_in_page: Page, base_url, path, where):
    logged_in_page.goto(base_url + path)
    expect(logged_in_page.locator("#admin-content")).to_be_visible()
    assert_accessible(logged_in_page, where)
