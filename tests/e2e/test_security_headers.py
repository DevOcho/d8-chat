"""
Verify the security headers we added during the audit are actually present
on responses from the running app. Catches regressions where someone
accidentally drops the ``@app.after_request`` hook or the CSP gets relaxed.
"""

from playwright.sync_api import Page

REQUIRED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _headers_for(page: Page, path: str) -> dict:
    response = page.goto(path)
    assert response is not None, f"no response from {path!r}"
    # Header keys are case-insensitive; lowercase for matching.
    return {k.lower(): v for k, v in response.headers.items()}


def test_login_page_has_security_headers(page: Page):
    headers = _headers_for(page, "/")
    for name, expected in REQUIRED_HEADERS.items():
        actual = headers.get(name.lower())
        assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"


def test_login_page_has_csp_with_no_third_party_origins(page: Page):
    headers = _headers_for(page, "/")
    csp = headers.get("content-security-policy", "")
    assert csp, "Content-Security-Policy header missing"
    # All third-party CDN origins should have been removed.
    assert "cdn.jsdelivr.net" not in csp, (
        f"CSP allows jsdelivr — should be vendored locally instead. Got: {csp!r}"
    )
    # Core directives we care about.
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ):
        assert directive in csp, f"CSP missing {directive!r}; got: {csp!r}"


def test_hsts_present_when_request_was_https(page: Page, base_url: str):
    """HSTS is only emitted when ``request.is_secure`` is True. Skip when
    the test is running against an http base URL."""
    if not base_url.startswith("https://"):
        import pytest

        pytest.skip("HSTS only emitted over HTTPS; skipping for http base URL")

    headers = _headers_for(page, "/")
    hsts = headers.get("strict-transport-security", "")
    assert "max-age=" in hsts, f"HSTS header missing or malformed: {hsts!r}"


def test_api_v1_responses_have_tight_csp(page: Page):
    """``/api/v1/*`` responses get ``default-src 'none'``, not the HTML CSP."""
    headers = _headers_for(page, "/api/v1/app-config")
    csp = headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp, (
        f"API CSP should be `default-src 'none'`; got: {csp!r}"
    )
