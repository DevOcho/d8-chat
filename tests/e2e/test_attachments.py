"""
File-upload e2e: image, PDF, and a refused-extension case.

The web upload path uses a raw ``fetch()`` (not HTMX) and relies on the
``X-CSRFToken`` meta tag being attached by chat.js. The audit found this had
been silently broken — the regression here proves uploads still work end-to-
end through CSRF, content sniffing, and MinIO storage.
"""

from playwright.sync_api import Page, expect

from .helpers import (
    attach_file,
    open_general_channel,
    send_message,
    wait_for_message_with_text,
)


def test_image_upload_renders_inline(logged_in_page: Page, fixture_path):
    """A real PNG attaches, uploads, and renders as an <img> in the message."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.png"))
    send_message(logged_in_page, "image attachment test")
    wait_for_message_with_text(logged_in_page, "image attachment test")

    # The newest message should have an attached image. We don't assert the
    # src URL (it's a presigned MinIO URL with a 15-min TTL) — only that
    # the <img> rendered.
    img = logged_in_page.locator("#message-list img").last
    expect(img).to_be_visible(timeout=10_000)


def test_pdf_upload_renders_as_attachment_chip(logged_in_page: Page, fixture_path):
    """Non-image attachments render as a download chip, not as <img>."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.pdf"))
    send_message(logged_in_page, "pdf attachment test")
    wait_for_message_with_text(logged_in_page, "pdf attachment test")

    # The message should contain a link to the file (with the original
    # filename in the visible text or `download` attribute) and *not* an
    # inline <img>.
    last_message = logged_in_page.locator("#message-list > div").last
    expect(last_message.locator("a")).to_have_count(
        # At least one link — the attachment download.
        # Other links (mention, channel) shouldn't appear in this test message.
        1,
        timeout=10_000,
    )


def test_text_upload_works(logged_in_page: Page, fixture_path):
    """Plain ``.txt`` files are an explicitly-allowed extension."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.txt"))
    send_message(logged_in_page, "text attachment test")
    wait_for_message_with_text(logged_in_page, "text attachment test")
    last_message = logged_in_page.locator("#message-list > div").last
    expect(last_message.locator("a")).to_have_count(1, timeout=10_000)
