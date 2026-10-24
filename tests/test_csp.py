# tests/test_csp.py
"""Content-Security-Policy headers per CSP_MODE, and the violation-report
endpoint (CSP_PLAN.md)."""

import json
import logging

import pytest

from app import CSP_ENFORCEABLE, _build_csp, _csp_headers, create_app


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
    assert target["script-src"] == "'self'"
    assert target["style-src"] == "'self'"
    assert target["style-src-attr"] == "'none'"
    for policy in (enforced, report_only):
        d = _directives(policy)
        assert d["report-uri"] == "/csp-report"
        assert d["report-to"] == "csp"


def test_enforce_mode_tightens_only_the_groups_the_code_is_ready_for(monkeypatch):
    monkeypatch.setitem(CSP_ENFORCEABLE, "scripts", True)
    monkeypatch.setitem(CSP_ENFORCEABLE, "styles", False)
    enforced, report_only = _csp_headers("", "enforce")
    d = _directives(enforced)
    assert d["script-src"] == "'self'"
    assert d["style-src"] == "'self' 'unsafe-inline'"
    assert _directives(report_only)["style-src-attr"] == "'none'"


def test_strict_policy_keeps_the_other_directives():
    d = _directives(
        _build_csp("http://minio.local", strict_scripts=True, strict_styles=True)
    )
    assert d["object-src"] == "'none'"
    assert d["base-uri"] == "'self'"
    assert d["frame-ancestors"] == "'none'"
    assert "http://minio.local" in d["img-src"]


def test_unknown_mode_fails_at_boot(monkeypatch):
    from config import TestConfig

    monkeypatch.setattr(TestConfig, "CSP_MODE", "strict-ish")
    with pytest.raises(RuntimeError, match="CSP_MODE"):
        create_app(config_class="config.TestConfig")


def test_html_pages_carry_both_headers_and_the_reporting_endpoint(client):
    res = client.get("/")
    assert "unsafe-inline" in res.headers["Content-Security-Policy"]
    assert "script-src 'self';" in res.headers["Content-Security-Policy-Report-Only"]
    assert res.headers["Reporting-Endpoints"] == 'csp="http://localhost/csp-report"'


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
