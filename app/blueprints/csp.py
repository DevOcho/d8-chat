"""Content-Security-Policy violation reports (CSP_PLAN.md, Phase 0).

Browsers POST here when a page breaks the policy, enforced or Report-Only.
Each report becomes one ``csp_violation`` log line, which is how the rollout
is watched: zero first-party violations for the soak window is the gate to
enforce the next directive group.

Accepts both formats in the wild: the legacy ``report-uri`` body
(``application/csp-report``, one ``{"csp-report": {...}}`` object) and the
Reporting API (``application/reports+json``, a list of reports). Anyone can
POST here, so it's rate-limited per IP, the body is capped, every logged value
is truncated and stripped of control characters, and violations caused by
browser extensions are dropped as noise.
"""

import json

from flask import Blueprint, current_app, request

from app import limiter

csp_bp = Blueprint("csp", __name__)

MAX_REPORT_BYTES = 16 * 1024
MAX_FIELD_CHARS = 200
# Extensions inject scripts and styles into every page they run on; their
# violations say nothing about the app.
NOISE_PREFIXES = (
    "chrome-extension",
    "moz-extension",
    "safari-extension",
    "safari-web-extension",
    "edge-extension",
)


def _clean(value):
    """A value safe for a single log line: printable and bounded."""
    if value is None:
        return ""
    text = str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_FIELD_CHARS]


def _normalize(payload):
    """Yield one dict per violation, whichever format the browser sent."""
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        body = payload["csp-report"]
        yield {
            "disposition": body.get("disposition"),
            "directive": body.get("effective-directive")
            or body.get("violated-directive"),
            "blocked": body.get("blocked-uri"),
            "source": body.get("source-file"),
            "line": body.get("line-number"),
            "sample": body.get("script-sample"),
            "page": body.get("document-uri"),
        }
        return
    if isinstance(payload, list):
        for report in payload:
            if not isinstance(report, dict) or report.get("type") != "csp-violation":
                continue
            body = report.get("body") or {}
            if not isinstance(body, dict):
                continue
            yield {
                "disposition": body.get("disposition"),
                "directive": body.get("effectiveDirective"),
                "blocked": body.get("blockedURL"),
                "source": body.get("sourceFile"),
                "line": body.get("lineNumber"),
                "sample": body.get("sample"),
                "page": body.get("documentURL") or report.get("url"),
            }


def _is_noise(violation):
    return any(
        str(violation.get(field) or "").startswith(NOISE_PREFIXES)
        for field in ("blocked", "source")
    )


@csp_bp.route("/csp-report", methods=["POST"])
@limiter.limit("120 per minute")
def csp_report():
    """Log the CSP violations in a browser's report. Always 204 when parsed."""
    if (request.content_length or 0) > MAX_REPORT_BYTES:
        return "", 413
    raw = request.get_data(cache=False)[: MAX_REPORT_BYTES + 1]
    if len(raw) > MAX_REPORT_BYTES:
        return "", 413
    try:
        payload = json.loads(raw)
    except ValueError:
        return "", 400
    for violation in _normalize(payload):
        if _is_noise(violation):
            continue
        current_app.logger.warning(
            "csp_violation disposition=%s directive=%s blocked=%s source=%s:%s "
            "page=%s sample=%s",
            *(
                _clean(violation.get(key))
                for key in (
                    "disposition",
                    "directive",
                    "blocked",
                    "source",
                    "line",
                    "page",
                    "sample",
                )
            ),
        )
    return "", 204
