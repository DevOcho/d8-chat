"""Tests for the ``static_url()`` template global.

Production nginx serves /css/ and /js/ with ``expires 1d`` while the HTML that
references them is served fresh, so an asset URL has to change when its content
does — otherwise a deploy's JS lands in browsers up to a day after the markup it
belongs to.
"""

import os
import re

import pytest

TOKEN_RE = re.compile(r"^\?v=[0-9a-f]{8}$")


@pytest.fixture
def static_url(app):
    """The registered template global, callable inside a request context."""
    return app.jinja_env.globals["static_url"]


def test_real_asset_gets_a_content_hash(app, static_url):
    with app.test_request_context():
        url = static_url("js/chat.js")
    path, _, query = url.partition("?")
    assert path == "/js/chat.js"  # static_url_path="" serves assets at the root
    assert TOKEN_RE.match(f"?{query}"), url


def test_token_is_stable_across_calls(app, static_url):
    with app.test_request_context():
        assert static_url("js/chat.js") == static_url("js/chat.js")


def test_different_assets_get_different_tokens(app, static_url):
    with app.test_request_context():
        first = static_url("js/chat.js")
        second = static_url("css/style.css")
    assert first.partition("?")[2] != second.partition("?")[2]


def test_missing_asset_degrades_to_a_plain_url(app, static_url):
    """A missing asset must 404 on its own, not 500 the page that includes it."""
    with app.test_request_context():
        url = static_url("js/does-not-exist.js")
    assert url == "/js/does-not-exist.js"
    assert "?v=" not in url


def test_token_follows_content_not_mtime(app, static_url, tmp_path):
    """Rewriting a file with identical bytes must not change its URL.

    The prod init container copies static files into the nginx volume with
    ``cp -r`` (no ``-p``), so mtimes differ per replica. An mtime-derived token
    would give each pod a different URL for identical bytes.
    """
    app.static_folder = str(tmp_path)
    app.debug = True  # re-read on mtime change instead of trusting the cache
    asset = tmp_path / "app.js"
    asset.write_text("console.log('v1')")

    with app.test_request_context():
        before = static_url("app.js")
        asset.write_text("console.log('v1')")  # same bytes, new mtime
        os.utime(asset, (1e9, 1e9))
        unchanged = static_url("app.js")
        asset.write_text("console.log('v2')")  # different bytes
        os.utime(asset, (2e9, 2e9))
        after = static_url("app.js")

    assert before.partition("?")[2] == unchanged.partition("?")[2]
    assert before.partition("?")[2] != after.partition("?")[2]


def test_edited_asset_repoints_without_a_restart_in_debug(app, static_url, tmp_path):
    app.static_folder = str(tmp_path)
    app.debug = True
    asset = tmp_path / "style.css"
    asset.write_text("body{color:red}")
    with app.test_request_context():
        before = static_url("style.css")
        asset.write_text("body{color:blue}")
        os.utime(asset, (3e9, 3e9))
        after = static_url("style.css")
    assert before != after


def test_rendered_pages_reference_versioned_assets(client):
    """The templates must actually use the helper, not bypass it."""
    body = client.get("/").get_data(as_text=True)
    assert re.search(r'href="/css/style\.css\?v=[0-9a-f]{8}"', body), body[:400]
    assert re.search(r'src="/js/local/htmx\.min\.js\?v=[0-9a-f]{8}"', body)


def test_no_template_bypasses_the_helper():
    """Guard against a new template hardcoding an unversioned asset path."""
    offenders = []
    for root, _dirs, files in os.walk("app/templates"):
        for name in files:
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    if re.search(
                        r'(?:href|src)="/(?:css|js|img|audio)/', line
                    ) or re.search(r"url_for\(['\"]static['\"]", line):
                        offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "These reference a static asset without a cache-busting token. Use "
        "{{ static_url('path/to/asset') }} instead:\n" + "\n".join(offenders)
    )
