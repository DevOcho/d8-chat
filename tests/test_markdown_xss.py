"""
Fuzz-style XSS coverage for the markdown filter.

The markdown pipeline in ``app/__init__.py`` does this:

    emoji → mentions → escape h1 → channels → extract code blocks →
    markdown.markdown → bleach.linkify + bleach.clean → reinsert mentions,
    channels, code blocks

The reinsertion step happens *after* bleach, so the mention/channel/code
substitutions effectively bypass sanitization. This file is the thing that
proves they're safe — if any of these tests start producing actually-renderable
dangerous HTML (live ``<script>`` tags, inline event handlers, ``javascript:``
URLs in attributes), the pipeline regressed.

Note: substrings like ``javascript:`` are FINE inside text content (a user
should be able to *talk about* JS in a chat); we only fail when they appear
in HTML attributes or as live tags.
"""

from html.parser import HTMLParser

import pytest


def _render(app, content: str) -> str:
    """Run user input through the production markdown filter."""
    with app.app_context():
        from flask import render_template_string

        return render_template_string("{{ x | markdown }}", x=content)


# Tag names that should never appear as live elements in chat output, even if
# the bleach allowlist is loosened by mistake later. ``img`` is intentionally
# omitted since the markdown filter should let ``![alt](src)`` work for legit
# image embeds (currently bleach strips it but that's a separate decision).
DANGEROUS_TAGS = frozenset(
    {
        "script",
        "iframe",
        "object",
        "embed",
        "svg",
        "form",
        "input",
        "meta",
        "link",
        "style",
        "base",
        "applet",
        "frame",
        "frameset",
    }
)


class _ThreatDetector(HTMLParser):
    """
    Walks rendered HTML and records anything that would actually execute in a
    browser. Escaped text like ``&lt;script&gt;alert(1)&lt;/script&gt;`` is
    harmless — only attempts that survived as real tags or attributes count.
    """

    def __init__(self):
        super().__init__()
        self.threats: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._inspect(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._inspect(tag, attrs)

    def _inspect(self, tag, attrs):
        if tag in DANGEROUS_TAGS:
            self.threats.append(f"<{tag}>")
        for name, value in attrs:
            lname = (name or "").lower()
            lvalue = (value or "").strip().lower()
            # Inline event handlers always execute.
            if lname.startswith("on"):
                self.threats.append(f"<{tag} {name}=...>")
            # URL-bearing attributes that resolve to script execution.
            if lname in {"href", "src", "action", "formaction", "xlink:href"}:
                if lvalue.startswith("javascript:") or lvalue.startswith(
                    "data:text/html"
                ):
                    self.threats.append(f"<{tag} {name}={value!r}>")


def _threats(html: str) -> list[str]:
    parser = _ThreatDetector()
    parser.feed(html)
    return parser.threats


# --- Plain HTML injection (should be stripped or escaped by bleach) ---


class TestRawHtmlIsStripped:
    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "<iframe src='javascript:alert(1)'></iframe>",
            "<svg onload=alert(1)>",
            "<a href='javascript:alert(1)'>click</a>",
            '<div onclick="alert(1)">click</div>',
            '<button formaction="javascript:alert(1)">x</button>',
            "<style>body{background:url('javascript:alert(1)')}</style>",
            "<meta http-equiv='refresh' content='0;url=javascript:alert(1)'>",
            "<link rel='stylesheet' href='javascript:alert(1)'>",
            "<base href='javascript:alert(1)//'>",
            "<form action='javascript:alert(1)'><input type=submit></form>",
        ],
    )
    def test_payload_neutralized(self, app, payload):
        out = _render(app, payload)
        threats = _threats(out)
        assert threats == [], f"survived render of {payload!r}: {out!r} → {threats}"


# --- Markdown link/image with javascript: scheme ---


class TestMarkdownLinkSchemes:
    def test_javascript_link_neutralized(self, app):
        out = _render(app, "[click](javascript:alert(1))")
        assert "javascript:" not in out.lower()

    def test_data_html_link_neutralized(self, app):
        out = _render(app, "[click](data:text/html,<script>alert(1)</script>)")
        assert "data:text/html" not in out.lower()
        assert "<script" not in out.lower()

    def test_javascript_image_neutralized(self, app):
        # img tag isn't in the bleach allowlist; the resulting <img> should
        # be escaped to text, not survive as an attribute-bearing element.
        out = _render(app, "![x](javascript:alert(1))")
        assert _threats(out) == [], out


# --- Code fence behaviour (the placeholders bypass bleach) ---


class TestCodeFenceEscaping:
    def test_script_inside_code_fence_is_escaped(self, app):
        out = _render(app, "```\n<script>alert(1)</script>\n```")
        # No live <script> in the output, only escaped text.
        assert _threats(out) == [], out
        assert "<script>" not in out

    def test_attr_list_does_not_inject_event_handler(self, app):
        # attr_list is part of markdown's `extra`. Code fences shouldn't be
        # subject to attr_list injection but let's make sure heading-level
        # attr_list can't smuggle handlers either.
        out = _render(app, '## title {: onclick="alert(1)"}')
        assert _threats(out) == [], out

    def test_attr_list_on_paragraph(self, app):
        out = _render(app, 'paragraph {: onclick="alert(1)"}')
        assert _threats(out) == [], out

    def test_html_inside_indented_code_block(self, app):
        out = _render(app, "    <script>alert(1)</script>")
        assert _threats(out) == [], out
        assert "<script>alert" not in out

    def test_inline_code_escapes_html(self, app):
        out = _render(app, "use `<script>alert(1)</script>` here")
        assert "<script>alert" not in out


# --- Mention / channel placeholder paths ---


class TestMentionPlaceholderSafety:
    def test_at_mention_with_html_chars_doesnt_match(self, app):
        # The mention regex is \w+ so HTML metachars can't enter the mention
        # link. They'll fall through to the markdown/bleach path instead.
        out = _render(app, "@<script>alert(1)</script>")
        assert _threats(out) == [], out

    def test_unknown_mention_left_as_text(self, app):
        # Unknown @whoever isn't substituted; it should appear as plain text
        # (or wrapped in an emphasis if the @ touches one), not as a link.
        out = _render(app, "@nonexistentuser123")
        assert "<a " not in out or "mention-link" not in out


class TestChannelPlaceholderSafety:
    def test_hashtag_with_html_doesnt_match(self, app):
        # Channel regex restricts to [a-zA-Z0-9_-]+ so HTML can't bleed in.
        out = _render(app, "#<script>alert(1)</script>")
        assert _threats(out) == [], out

    def test_hashtag_to_search_link_is_safe(self, app):
        out = _render(app, "#trending-topic")
        # Either rendered as a plain # text or as a sanitized hashtag link
        # — but no dangerous attrs.
        assert _threats(out) == [], out


# --- Polyglot / mixed-content payloads ---


class TestPolyglots:
    def test_html_inside_code_fence_then_outside(self, app):
        out = _render(
            app,
            "```\n<script>alert(1)</script>\n```\n\n<script>alert(2)</script>",
        )
        # Both should be neutralized — the in-fence one via escaping, the
        # outside one via bleach stripping.
        assert "<script>alert" not in out

    def test_dangling_open_tag(self, app):
        out = _render(app, "<script>alert(1)\n\nstill text")
        assert _threats(out) == [], out

    def test_unicode_obfuscation(self, app):
        # Various tricks attackers use to dodge naive filters.
        for payload in [
            "<\x00script>alert(1)</script>",
            "<scr\x00ipt>alert(1)</script>",
            "<SCRIPT>alert(1)</SCRIPT>",
            "<sCrIpT>alert(1)</sCrIpT>",
        ]:
            out = _render(app, payload)
            assert "alert(1)" not in out or "<script" not in out.lower(), (
                f"payload {payload!r} produced: {out!r}"
            )

    def test_nested_quote_attempt(self, app):
        # Try to break out of an attribute context via bleach's parser.
        out = _render(app, '"><script>alert(1)</script><"')
        assert _threats(out) == [], out

    def test_bleach_link_callback_adds_target_blank(self, app):
        # The linkify callback should always add target=_blank rel=noopener.
        out = _render(app, "Check out https://example.com today.")
        assert 'target="_blank"' in out
        assert "noopener" in out
