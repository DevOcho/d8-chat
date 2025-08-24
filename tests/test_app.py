# tests/test_app.py

from markupsafe import Markup


def test_markdown_filter(app):
    """
    Tests the custom markdown filter's functionality.
    - Converts markdown to HTML
    - Sanitizes malicious HTML
    - Converts emoji shortcodes
    - Makes links clickable
    """
    # The filter is part of the app's Jinja environment
    markdown_filter = app.jinja_env.filters["markdown"]

    # Test case 1: Basic markdown and emoji
    md_input_1 = "Hello **world**! :smile:"
    html_output_1 = markdown_filter(md_input_1)
    assert "<strong>world</strong>" in html_output_1
    assert "😄" in html_output_1  # Check for unicode emoji

    # Test case 2: Malicious script tag (sanitization)
    md_input_2 = "This is a <script>alert('hack')</script> test."
    html_output_2 = markdown_filter(md_input_2)
    assert "<script>" not in html_output_2
    assert "&lt;script&gt;" in html_output_2  # Should be escaped

    # Test case 3: Linkification
    md_input_3 = "Check out google.com for more."
    html_output_3 = markdown_filter(md_input_3)
    # The assertion is now split to be more robust against attribute reordering.
    assert '<a href="http://google.com"' in html_output_3
    assert 'target="_blank"' in html_output_3
    assert 'rel="noopener noreferrer"' in html_output_3
    assert ">google.com</a>" in html_output_3

    # Test case 4: Ensure it returns a Markup object
    assert isinstance(html_output_1, Markup)

    # Test case 5: Fenced code block
    md_input_5 = "Check out this code:\n```python\nprint('Hello')\n```"
    html_output_5 = markdown_filter(md_input_5)
    assert '<div class="codehilite">' in html_output_5
    assert '<span class="nb">print</span>' in html_output_5


def test_highlight_filter(app):
    """
    Covers: Custom 'highlight' template filter.
    """
    highlight_filter = app.jinja_env.filters["highlight"]
    text = "This is a test sentence."
    query = "test"
    result = highlight_filter(text, query)
    assert result == "This is a <mark>test</mark> sentence."
