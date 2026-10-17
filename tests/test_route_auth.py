"""Every route must require authentication, except an explicit public allowlist.

This is the regression net for the "no unauthenticated routes" requirement. It
checks the same thing two ways, because either check alone has a blind spot:

* ``test_no_route_is_missing_an_auth_guard`` walks ``app.view_functions`` and
  identifies each decorator layer by its *code object* (file + co_name), which
  functools.wraps does not overwrite. This catches a route registered with the
  guard stacked in the wrong order — ``@login_required`` placed *above*
  ``@bp.route`` registers the undecorated function, which looks correct in
  source but ships wide open.
* ``test_every_guarded_route_rejects_an_anonymous_request`` actually calls each
  route with no session cookie and no bearer token and demands a redirect to the
  login page, a 401, or a 403 — proving the guard fires at request time.

Adding a route requires no change here. Adding a *public* route deliberately
means adding it to PUBLIC_ENDPOINTS with a reason, which is the point: the
allowlist is the reviewable list of the app's pre-auth attack surface.
"""

import os
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from app.models import Channel, Conversation, Message, User

# --- The pre-auth attack surface -------------------------------------------
#
# Every entry is reachable without credentials by design. Keep the reason
# current; anything added here should be assumed to be internet-facing.
PUBLIC_ENDPOINTS = {
    # Landing page carrying the login form, and the login POST itself.
    "auth.index": "landing page + login form",
    "auth.login_page": "legacy /login, redirects to the landing page",
    "auth.login": "credential submission (rate limited 5/min, 50/hr)",
    "auth.logout": "clears a session; POST-only so it can't be CSRF'd",
    # SSO handshake: both ends are pre-auth by definition.
    "auth.sso_login": "redirect to the OIDC provider",
    "auth.authorize": "OIDC callback (validates state + nonce)",
    # Password reset: pre-auth by definition, guarded by a signed token.
    "auth.forgot_password_form": "email entry form",
    "auth.forgot_password": "reset request (rate limited 3/min, 20/hr)",
    "auth.reset_password_form": "signed-token gated (400s on a bad token)",
    "auth.reset_password": "signed-token gated (rate limited 5/min, 20/hr)",
    # Mobile pre-login needs.
    "api_v1.api_login": "credential submission (rate limited 5/min, 50/hr)",
    "api_v1.get_app_config": "branding + SSO URL for the app launch screen",
    "api_v1.sso_exchange": "trades an OIDC code for an API token",
    # Not decorator-guarded, but authenticated inside the view/handler.
    # Asserted separately below so they can't quietly lose their check.
    "api_v1.internal_notify": "X-Internal-Key shared secret, compared in constant time",
    "api_v1.uptime_kuma_webhook": "Bearer UPTIME_KUMA_WEBHOOK_KEY, compared in constant time",
    "chat": "WebSocket: session check + Origin check inside the handler",
    "api_ws": "WebSocket: bearer token via Sec-WebSocket-Protocol",
    # Infrastructure.
    "main.healthz": "k8s readiness probe; reports component status only",
    "static": "static assets",
}

GUARDS = {
    ("app/routes.py", "wrapped_view"): "login_required",
    ("app/blueprints/admin.py", "wrapped_view"): "admin_required",
    ("app/blueprints/admin_oncall.py", "wrapped_view"): "oncall_admin_required",
    ("app/blueprints/api_v1.py", "decorated"): "api_token_required",
}

# Routes the anonymous-request probe can't drive meaningfully.
UNPROBEABLE = {
    "chat",  # WebSocket upgrade, not an HTTP request
    "api_ws",
}

# flask_sock registers its routes at import time. If app.routes was imported
# before the first sock.init_app() call the handlers land on flask_sock's own
# blueprint and are named "__flask_sock.chat"; if after, they attach straight to
# the app as "chat". Which one happens depends on import order (pytest collecting
# a module that imports app.routes flips it), so normalize before matching.
SOCK_BP_PREFIX = "__flask_sock."


def _norm(endpoint):
    """Endpoint name with flask_sock's blueprint prefix stripped."""
    return (
        endpoint[len(SOCK_BP_PREFIX) :]
        if endpoint.startswith(SOCK_BP_PREFIX)
        else endpoint
    )


def _guards_on(view_fn):
    """Names of the auth guards wrapping ``view_fn``, identified by code object.

    functools.wraps copies __name__/__qualname__ but not __code__, so co_name +
    co_filename still identify the decorator that actually wrapped the view.
    """
    found = set()
    fn, seen = view_fn, set()
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        code = getattr(fn, "__code__", None)
        if code:
            rel = os.path.relpath(code.co_filename, os.getcwd())
            if (rel, code.co_name) in GUARDS:
                found.add(GUARDS[(rel, code.co_name)])
        fn = getattr(fn, "__wrapped__", None)
    return found


def test_no_route_is_missing_an_auth_guard(app):
    """Every registered route is guarded or explicitly public."""
    unguarded = sorted(
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if _norm(rule.endpoint) not in PUBLIC_ENDPOINTS
        and not _guards_on(app.view_functions.get(rule.endpoint))
    )
    assert not unguarded, (
        "These routes have no login_required / admin_required / oncall_admin_required / "
        "api_token_required "
        "layer on the function registered in app.view_functions. Either add a guard "
        "(below @route, not above it) or add the endpoint to PUBLIC_ENDPOINTS with a "
        f"reason: {unguarded}"
    )


def test_public_allowlist_has_no_stale_entries(app):
    """PUBLIC_ENDPOINTS must not name routes that no longer exist.

    The two WebSocket endpoints are exempt: flask_sock registers them at import
    time against whichever app existed then, so only the first app created in
    the process carries them. They're asserted directly further down instead.
    """
    live = {_norm(rule.endpoint) for rule in app.url_map.iter_rules()}
    stale = sorted(set(PUBLIC_ENDPOINTS) - live - UNPROBEABLE)
    assert not stale, f"PUBLIC_ENDPOINTS names routes that no longer exist: {stale}"


def _sample_url(rule):
    """Fill a rule's parameters with values that exist in the seeded test DB."""
    samples = {"conv_id_str": "channel_1", "token": "bogus", "filename": "js/chat.js"}
    url = rule.rule
    for arg in rule.arguments:
        val = samples.get(arg, "1")
        for form in (f"<{arg}>", f"<int:{arg}>", f"<path:{arg}>", f"<string:{arg}>"):
            url = url.replace(form, val)
    return url


def _is_refusal(status_code, location):
    """True when a response turned an anonymous caller away.

    401/403, or a redirect whose destination is the login page. A 3xx to
    anywhere else doesn't count — that's a guard handing the caller onward to
    something it was allowed to reach.
    """
    if status_code in (401, 403):
        return True
    if 300 <= status_code < 400:
        path = urlsplit(location).path or "/"
        return path.rstrip("/") in ("", "/login")
    return False


def _anonymous_route_cases(app):
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        endpoint = _norm(rule.endpoint)
        if endpoint in PUBLIC_ENDPOINTS or endpoint in UNPROBEABLE:
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield endpoint, method, _sample_url(rule)


def test_every_guarded_route_rejects_an_anonymous_request(app, client):
    """Drive each non-public route with no session and no token.

    Guards must answer with a redirect to the login page, a 401, or a 403 — never
    a 2xx, and never a redirect somewhere useful. The ids referenced by
    _sample_url are seeded by the test_db fixture, so a guarded view can't pass
    this by 404ing on a missing row instead of refusing the caller.
    """
    with app.app_context():
        author = User.create(username="route-audit", email="ra@example.com")
        conv = Conversation.get(conversation_id_str="channel_1")
        Message.create(user=author, conversation=conv, content="audit fixture")
        assert Channel.get_by_id(1)

    cases = list(_anonymous_route_cases(app))
    # Guard against the test passing vacuously if route collection ever breaks.
    assert len(cases) > 90, f"only {len(cases)} routes probed — collection is broken"

    leaked = []
    for endpoint, method, url in cases:
        res = client.open(url, method=method, json={}, follow_redirects=False)
        location = res.headers.get("Location", "")
        if not _is_refusal(res.status_code, location):
            leaked.append(
                f"{method} {url} ({endpoint}) -> {res.status_code} {location}"
            )

    assert not leaked, "Routes answered an unauthenticated caller:\n" + "\n".join(
        leaked
    )


# --- The routes that authenticate inside the view/handler --------------------


def test_internal_notify_requires_the_shared_secret(client):
    body = {"channel_name": "general", "message": "hi"}
    assert client.post("/api/v1/internal/notify", json=body).status_code == 401
    res = client.post(
        "/api/v1/internal/notify", json=body, headers={"X-Internal-Key": "wrong"}
    )
    assert res.status_code == 401


def test_internal_notify_fails_closed_when_no_key_is_configured(app, client):
    """An unset INTERNAL_NOTIFY_KEY must lock the endpoint, not open it."""
    app.config["INTERNAL_NOTIFY_KEY"] = None
    res = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "general", "message": "hi"},
        headers={"X-Internal-Key": ""},
    )
    assert res.status_code == 401


def test_uptime_kuma_webhook_requires_the_bearer_key(client):
    body = {"heartbeat": {"status": 0}, "monitor": {"id": 1, "name": "x"}}
    assert client.post("/api/v1/integrations/uptime-kuma", json=body).status_code == 401
    res = client.post(
        "/api/v1/integrations/uptime-kuma",
        json=body,
        headers={"Authorization": "Bearer wrong"},
    )
    assert res.status_code == 401


def test_uptime_kuma_webhook_fails_closed_when_no_key_is_configured(app, client):
    """An unset UPTIME_KUMA_WEBHOOK_KEY must lock the endpoint, not open it."""
    app.config["UPTIME_KUMA_WEBHOOK_KEY"] = None
    res = client.post(
        "/api/v1/integrations/uptime-kuma",
        json={"msg": "hi"},
        headers={"Authorization": "Bearer "},
    )
    assert res.status_code == 401


@pytest.mark.parametrize(
    ("url", "config_key", "header"),
    [
        (
            "/api/v1/integrations/uptime-kuma",
            "UPTIME_KUMA_WEBHOOK_KEY",
            "Authorization",
        ),
        ("/api/v1/internal/notify", "INTERNAL_NOTIFY_KEY", "X-Internal-Key"),
    ],
)
def test_shared_secret_endpoints_reject_non_ascii_keys(
    app, client, url, config_key, header
):
    """compare_digest on str raises for non-ASCII; that must be a 401, not 500."""
    app.config[config_key] = "the-real-key"
    value = "Bearer é" if header == "Authorization" else "é"
    res = client.post(url, json={"msg": "hi"}, headers={header: value})
    assert res.status_code == 401


def _mock_ws():
    ws = Mock()
    ws.receive.return_value = None
    return ws


def test_chat_websocket_rejects_an_anonymous_connection(app):
    from app.routes import chat

    ws = _mock_ws()
    with app.test_request_context("/ws/chat", headers={"Origin": "http://localhost"}):
        chat(ws)
    ws.close.assert_called_once()
    assert ws.close.call_args.kwargs["reason"] == 1008


@pytest.mark.parametrize(
    ("mode", "shut_down"), [("werkzeug", True), ("gunicorn", False)]
)
def test_websocket_reject_keeps_the_dev_server_from_corrupting_the_close(
    app, mode, shut_down
):
    """Werkzeug writes an HTTP response after the handler returns; browsers read
    it as a bad frame and report 1006, which the client retries forever."""
    import socket

    from app.routes import chat

    ws = _mock_ws()
    ws.mode = mode
    with app.test_request_context("/ws/chat", headers={"Origin": "http://localhost"}):
        chat(ws)
    assert ws.close.call_args.kwargs["reason"] == 1008
    if shut_down:
        ws.sock.shutdown.assert_called_once_with(socket.SHUT_WR)
    else:
        ws.sock.shutdown.assert_not_called()


def test_chat_websocket_rejects_a_foreign_origin(app):
    """A valid session is not enough — a cross-site origin must still be closed."""
    from app.routes import chat

    ws = _mock_ws()
    with app.test_request_context(
        "/ws/chat", headers={"Origin": "https://evil.example.com"}
    ) as ctx:
        ctx.session["user_id"] = 1
        chat(ws)
    ws.close.assert_called_once()
    assert ws.close.call_args.kwargs["reason"] == 1008


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Sec-WebSocket-Protocol": "d8_sec"},
        {"Sec-WebSocket-Protocol": "d8_sec_bad"},
    ],
    ids=["no-header", "marker-only", "invalid-token"],
)
def test_api_websocket_rejects_a_missing_or_bad_token(app, headers):
    from app.routes import api_ws

    ws = _mock_ws()
    with app.test_request_context("/ws/api/v1", headers=headers):
        api_ws(ws)
    ws.close.assert_called_once()
    assert ws.close.call_args.kwargs["reason"] == 1008
