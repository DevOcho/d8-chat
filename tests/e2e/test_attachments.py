"""
File-upload e2e: image, PDF, and plain-text attachments.

The web upload path uses a raw ``fetch()`` (not HTMX) and relies on the
``X-CSRFToken`` meta tag being attached by chat.js. The audit found this had
been silently broken — the regression here proves uploads still work end-to-
end through CSRF, content sniffing, and MinIO storage.
"""

from playwright.sync_api import Page, expect

from .helpers import (
    attach_file,
    message_container,
    open_general_channel,
    send_message,
    unique_text,
    wait_for_message_with_text,
)


def test_image_upload_renders_inline(logged_in_page: Page, fixture_path):
    """A real PNG attaches, uploads, and renders as an <img> in the message."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.png"))
    text = unique_text("image attachment test")
    send_message(logged_in_page, text)
    wait_for_message_with_text(logged_in_page, text)

    # The message should have an attached image. We don't assert the src URL
    # (it's a presigned MinIO URL with a 15-min TTL) — only that the <img>
    # rendered.
    img = message_container(logged_in_page, text).locator(".attachment-grid img")
    expect(img).to_be_visible(timeout=10_000)


def test_pdf_upload_renders_as_attachment_chip(logged_in_page: Page, fixture_path):
    """Non-image attachments render as a download chip, not as <img>."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.pdf"))
    text = unique_text("pdf attachment test")
    send_message(logged_in_page, text)
    wait_for_message_with_text(logged_in_page, text)

    # The message should contain exactly one link — the attachment download,
    # carrying the original filename — and *not* an inline image grid.
    message = message_container(logged_in_page, text)
    expect(message.locator("a")).to_have_count(1, timeout=10_000)
    expect(message.locator("a[download='tiny.pdf']")).to_have_count(1)
    expect(message.locator(".attachment-grid")).to_have_count(0)


def test_text_upload_works(logged_in_page: Page, fixture_path):
    """Plain ``.txt`` files are an explicitly-allowed extension."""
    open_general_channel(logged_in_page)
    attach_file(logged_in_page, fixture_path("tiny.txt"))
    text = unique_text("text attachment test")
    send_message(logged_in_page, text)
    wait_for_message_with_text(logged_in_page, text)
    message = message_container(logged_in_page, text)
    expect(message.locator("a[download='tiny.txt']")).to_have_count(1, timeout=10_000)
