# tests/test_csp.py
"""Content-Security-Policy headers per CSP_MODE, and the violation-report
endpoint (CSP_PLAN.md)."""

import json
import logging
import re

import pytest

from app import (
    CSP_ENFORCEABLE,
    EMOJI_PICKER_STYLE_HASH,
    _build_csp,
    _csp_headers,
    create_app,
)

PICKER = f"'{EMOJI_PICKER_STYLE_HASH}'"


def _directives(policy):
    return {
        part.split(" ", 1)[0]: part.split(" ", 1)[1] if " " in part else ""
        for part in (p.strip() for p in policy.split(";"))
        if part
    }


# --- Policies --------------------------------------------------------------------


def test_legacy_policy_is_the_loose_one_without_reporting():
    enforced, report_only = _csp_headers("", "legacy")
    d = _directives(enforced)
    assert d["script-src"] == "'self' 'unsafe-inline' 'unsafe-eval'"
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert "report-uri" not in d
    assert report_only is None


def test_report_mode_enforces_loose_and_reports_the_full_target():
    enforced, report_only = _csp_headers("", "report")
    assert "'unsafe-inline'" in _directives(enforced)["script-src"]
    target = _directives(report_only)
    assert target["script-src"] == "'self' 'report-sample'"
    assert target["style-src"] == f"'self' {PICKER} 'report-sample'"
    assert target["style-src-elem"] == f"'self' {PICKER} 'report-sample'"
    assert target["style-src-attr"] == "'none'"
    for policy in (enforced, report_only):
        d = _directives(policy)
        assert d["report-uri"] == "/csp-report"
        assert d["report-to"] == "csp"


def test_enforce_mode_tightens_only_the_groups_the_code_is_ready_for(monkeypatch):
    monkeypatch.setitem(CSP_ENFORCEABLE, "scripts", True)
    monkeypatch.setitem(CSP_ENFORCEABLE, "style_elements", False)
    monkeypatch.setitem(CSP_ENFORCEABLE, "style_attributes", False)
    enforced, report_only = _csp_headers("", "enforce")
    d = _directives(enforced)
    assert d["script-src"] == "'self' 'report-sample'"
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert "style-src-elem" not in d
    assert _directives(report_only)["style-src-attr"] == "'none'"


def test_strict_style_elements_with_loose_attributes(monkeypatch):
    """Phase 5 on its own: <style> by hash only, style= attributes still
    allowed. The fallback keeps 'unsafe-inline' without the hash, which would
    switch it off for browsers that only know style-src."""
    monkeypatch.setitem(CSP_ENFORCEABLE, "style_elements", True)
    monkeypatch.setitem(CSP_ENFORCEABLE, "style_attributes", False)
    d = _directives(_csp_headers("", "enforce")[0])
    assert d["style-src-elem"] == f"'self' {PICKER} 'report-sample'"
    assert d["style-src-attr"] == "'unsafe-inline'"
    assert d["style-src"] == "'self' 'unsafe-inline'"


def test_enforce_mode_enforces_what_the_code_is_ready_for_today():
    enforced, _ = _csp_headers("", "enforce")
    d = _directives(enforced)
    for group, ready in CSP_ENFORCEABLE.items():
        strict = {
            "scripts": d["script-src"] == "'self' 'report-sample'",
            "style_elements": d.get("style-src-elem")
            == f"'self' {PICKER} 'report-sample'",
            "style_attributes": d.get("style-src-attr") == "'none'",
        }[group]
        assert strict is ready, group


def test_enforce_scripts_mode_leaves_styles_loose():
    enforced, report_only = _csp_headers("", "enforce-scripts")
    d = _directives(enforced)
    assert d["script-src"] == "'self' 'report-sample'"
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert "style-src-attr" not in d
    assert _directives(report_only)["style-src-attr"] == "'none'"


def test_strict_policy_keeps_the_other_directives():
    d = _directives(
        _build_csp(
            "http://minio.local",
            strict_scripts=True,
            strict_style_elements=True,
            strict_style_attributes=True,
        )
    )
    assert d["object-src"] == "'none'"
    assert d["base-uri"] == "'self'"
    assert d["frame-ancestors"] == "'none'"
    assert d["frame-src"] == "'none'"
    assert d["worker-src"] == "'self'"
    assert d["manifest-src"] == "'self'"
    assert "http://minio.local" in d["img-src"]


def test_strict_script_src_carries_the_nonce_in_both_headers():
    enforced, report_only = _csp_headers("", "enforce", nonce="abc123")
    for policy in (enforced, report_only):
        assert (
            _directives(policy)["script-src"] == "'self' 'nonce-abc123' 'report-sample'"
        )


@pytest.mark.parametrize("mode", ["legacy", "report"])
def test_loose_script_src_never_carries_the_nonce(mode):
    """A nonce next to 'unsafe-inline' switches 'unsafe-inline' off."""
    enforced, report_only = _csp_headers("", mode, nonce="abc123")
    assert "nonce-" not in enforced
    if report_only:
        assert "'nonce-abc123'" in _directives(report_only)["script-src"]


@pytest.mark.parametrize("mode", ["legacy", "report", "enforce", "enforce-scripts"])
def test_cloudflare_analytics_is_off_by_default(mode):
    for policy in filter(None, _csp_headers("", mode, nonce="abc123")):
        assert "cloudflareinsights" not in policy


@pytest.mark.parametrize("mode", ["legacy", "report", "enforce", "enforce-scripts"])
def test_cloudflare_analytics_lets_the_beacon_through_every_policy(mode):
    policies = _csp_headers("", mode, nonce="abc123", cloudflare_analytics=True)
    for policy in filter(None, policies):
        d = _directives(policy)
        assert "https://static.cloudflareinsights.com" in d["script-src"].split()
        assert d["connect-src"] == "'self' https://cloudflareinsights.com"
    if mode == "enforce":
        assert _directives(policies[0])["script-src"] == (
            "'self' https://static.cloudflareinsights.com 'nonce-abc123' 'report-sample'"
        )


def test_cloudflare_analytics_setting_reaches_the_headers(app, client):
    app.config["CSP_CLOUDFLARE_ANALYTICS"] = True
    try:
        res = client.get("/")
    finally:
        app.config["CSP_CLOUDFLARE_ANALYTICS"] = False
    for name in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
        assert "https://static.cloudflareinsights.com" in res.headers[name]
    assert (
        "cloudflareinsights" not in client.get("/").headers["Content-Security-Policy"]
    )


def test_unknown_mode_fails_at_boot(monkeypatch):
    from config import TestConfig

    monkeypatch.setattr(TestConfig, "CSP_MODE", "strict-ish")
    with pytest.raises(RuntimeError, match="CSP_MODE"):
        create_app(config_class="config.TestConfig")


NONCE = re.compile(r"'nonce-([A-Za-z0-9_-]{22})'")


def test_html_pages_carry_both_headers_and_the_reporting_endpoint(client):
    res = client.get("/")
    assert "unsafe-inline" in res.headers["Content-Security-Policy"]
    target = _directives(res.headers["Content-Security-Policy-Report-Only"])
    assert re.fullmatch(f"'self' {NONCE.pattern} 'report-sample'", target["script-src"])
    assert res.headers["Reporting-Endpoints"] == 'csp="http://localhost/csp-report"'


def test_every_response_gets_a_fresh_nonce_shared_by_its_two_headers(app, client):
    app.config["CSP_MODE"] = "enforce"
    try:
        responses = [client.get("/") for _ in range(2)]
    finally:
        app.config["CSP_MODE"] = "report"
    nonces = []
    for res in responses:
        found = {
            NONCE.search(res.headers[name]).group(1)
            for name in (
                "Content-Security-Policy",
                "Content-Security-Policy-Report-Only",
            )
        }
        assert len(found) == 1
        nonces += found
    assert nonces[0] != nonces[1]


def test_api_responses_keep_their_own_policy_and_no_report_only(client):
    res = client.get("/api/v1/app-config")
    assert (
        res.headers["Content-Security-Policy"]
        == "default-src 'none'; frame-ancestors 'none'"
    )
    assert "Content-Security-Policy-Report-Only" not in res.headers


# --- Reports -------------------------------------------------------------------------


LEGACY_REPORT = {
    "csp-report": {
        "document-uri": "https://chat.example/chat",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "blocked-uri": "inline",
        "source-file": "https://chat.example/chat",
        "line-number": 42,
        "script-sample": "document.body.dispatchEvent(",
        "disposition": "report",
    }
}

REPORTING_API = [
    {
        "type": "csp-violation",
        "url": "https://chat.example/admin/",
        "body": {
            "documentURL": "https://chat.example/admin/",
            "effectiveDirective": "style-src-attr",
            "blockedURL": "inline",
            "sourceFile": "https://chat.example/admin/",
            "lineNumber": 7,
            "sample": "display: none",
            "disposition": "report",
        },
    },
    {"type": "deprecation", "body": {"id": "x"}},
]


def _post(client, payload, content_type):
    return client.post(
        "/csp-report", data=json.dumps(payload), content_type=content_type
    )


def test_legacy_report_is_logged(client, caplog):
    with caplog.at_level(logging.WARNING):
        res = _post(client, LEGACY_REPORT, "application/csp-report")
    assert res.status_code == 204
    lines = [
        r.getMessage() for r in caplog.records if "csp_violation" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "directive=script-src-elem" in lines[0]
    assert "source=https://chat.example/chat:42" in lines[0]


def test_reporting_api_reports_are_logged_and_other_types_ignored(client, caplog):
    with caplog.at_level(logging.WARNING):
        res = _post(client, REPORTING_API, "application/reports+json")
    assert res.status_code == 204
    lines = [
        r.getMessage() for r in caplog.records if "csp_violation" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "directive=style-src-attr" in lines[0]


def test_extension_noise_is_dropped(client, caplog):
    noisy = {
        "csp-report": {
            **LEGACY_REPORT["csp-report"],
            "source-file": "chrome-extension://abc/inject.js",
        }
    }
    with caplog.at_level(logging.WARNING):
        _post(client, noisy, "application/csp-report")
    assert not [r for r in caplog.records if "csp_violation" in r.getMessage()]


def test_log_lines_are_single_line_and_bounded(client, caplog):
    evil = {
        "csp-report": {
            **LEGACY_REPORT["csp-report"],
            "script-sample": "x\ncsp_violation forged " + "y" * 500,
        }
    }
    with caplog.at_level(logging.WARNING):
        _post(client, evil, "application/csp-report")
    (line,) = [
        r.getMessage() for r in caplog.records if "csp_violation" in r.getMessage()
    ]
    assert "\n" not in line
    assert len(line) < 1500


def test_oversized_and_malformed_reports_are_refused(client):
    assert (
        client.post(
            "/csp-report", data="x" * 20000, content_type="application/csp-report"
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/csp-report", data="{not json", content_type="application/csp-report"
        ).status_code
        == 400
    )


def test_reports_need_no_session_or_csrf_token(app):
    app.config["WTF_CSRF_ENABLED"] = True
    with app.test_client() as anonymous:
        assert (
            _post(anonymous, LEGACY_REPORT, "application/csp-report").status_code == 204
        )
