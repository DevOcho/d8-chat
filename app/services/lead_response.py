# app/services/lead_response.py
"""Accept or reject one of D8's lead offers from D8-Chat (ONCALL_PLAN.md,
"Accept and Reject").

D8 offers each website lead to one SDR at a time and pages that person here
(``handle_page_request`` with ``escalate`` false and ``offer`` true, the
ka-ching). With ``D8_LEAD_RESPONSE_URL`` set, the web pager and the incident
card answer such a lead offer with Accept and Reject instead of Acknowledge
(``oncall_service.lead_response_enabled``), and D8-Chat's server sends
the answer to D8 (``POST /oncall/incidents/<id>/accept|reject`` calls
``respond``)::

    POST {D8_LEAD_RESPONSE_URL}
    X-Internal-Key: <INTERNAL_NOTIFY_KEY>
    {"key": "<the page key D8 sent>", "email": "<who pressed>",
     "action": "accept" | "reject"}

D8 answers ``200 {"ok": true, "status": "accepted" | "rejected", "lead_url"}``,
or ``{"error": <code>, "message"?}`` with ``401 unauthorized``, ``403
not_yours``, ``404 unknown_offer`` or ``409 offer_closed``. What D8-Chat does
with each:

* Answered (200): it resolves the incident itself, as D8's own
  ``POST /api/v1/internal/page/resolve`` does (D8 sends that too, and whichever
  arrives second finds nothing open), so the page stops at once on every screen
  and the card reads "Accepted in D8 by ...".
* Refused (403, 404 or 409 with its code): there is nothing left for this
  person to answer, so the page is acknowledged, without a post in the channel,
  and stops instead of ringing on.
* Anything else (no answer within ``TIMEOUT_SECONDS``, a network error, a 401,
  a 5xx, an answer it doesn't understand): D8 never took the answer, so the
  page keeps ringing and the person can try again.
"""

from dataclasses import asdict, dataclass

import requests
from flask import current_app

from app.services import oncall_service

ACTIONS = ("accept", "reject")
# D8's ``status`` on a 200, and the resolve reason it becomes here.
ANSWERED = ("accepted", "rejected")
# How long D8 has to answer before the person is told to try again.
TIMEOUT_SECONDS = 5
# D8's refusals: (HTTP status, error code) -> what the person is told.
REFUSALS = {
    (403, "not_yours"): "D8 says this lead was offered to someone else.",
    (404, "unknown_offer"): (
        "D8 has no offer for this page, so there is nothing to accept or reject."
    ),
    (409, "offer_closed"): "This offer has already closed in D8.",
}
# The longest message from D8 shown as it is; a longer one gets ours.
MAX_D8_MESSAGE = 200
ANSWER_MESSAGES = {
    "accepted": "The lead is yours.",
    "rejected": "It goes to the next person.",
}


@dataclass
class Outcome:
    """What became of an answer, as the pager and the card show it.

    ``ok``: D8 took the answer (``status`` says which, ``lead_url`` links the
    lead). ``stopped``: the page no longer rings (answered, or nothing left to
    answer); False means D8 never took it and it keeps ringing. ``code`` is D8's
    refusal code. ``http_status`` is what D8-Chat answers the browser with.
    """

    ok: bool
    stopped: bool
    message: str
    incident_id: int
    status: str | None = None
    lead_url: str | None = None
    code: str | None = None
    http_status: int = 200

    def to_dict(self):
        data = asdict(self)
        del data["http_status"]
        return data


def respond(incident, user, action):
    """Send ``user``'s ``action`` (accept or reject) on lead page ``incident``
    to D8 and act on D8's answer. Never raises; returns an ``Outcome``.

    The caller has checked that the feature is on, that ``incident`` is an open
    lead page and that ``user`` was paged for it.
    """
    payload = {
        "key": oncall_service.page_key(incident),
        "email": user.email,
        "action": action,
    }
    try:
        res = requests.post(
            current_app.config["D8_LEAD_RESPONSE_URL"],
            json=payload,
            headers={"X-Internal-Key": current_app.config["INTERNAL_NOTIFY_KEY"]},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        _log_failure(incident, action, "timed out")
        return _not_sent(incident, "D8 didn't answer in time. Try again.")
    except requests.RequestException as exc:
        _log_failure(incident, action, type(exc).__name__)
        return _not_sent(incident, "Couldn't reach D8. Try again.")

    body = _json(res)
    if res.status_code == 200 and body.get("ok") is True:
        if body.get("status") in ANSWERED:
            return _answered(incident, user, body)
    refusal = REFUSALS.get((res.status_code, body.get("error")))
    if refusal is not None:
        return _refused(incident, user, body, refusal)
    if res.status_code == 401:
        current_app.logger.error(
            "D8 refused D8-Chat's X-Internal-Key for the %s on incident %s: "
            "INTERNAL_NOTIFY_KEY doesn't match D8's",
            action,
            incident.id,
        )
        return _not_sent(
            incident,
            "D8 didn't accept D8-Chat's key, so your answer wasn't sent. "
            "Tell an admin, and answer the lead in D8.",
        )
    _log_failure(incident, action, f"HTTP {res.status_code}")
    if res.status_code >= 500:
        message = f"D8 had a problem (HTTP {res.status_code}). Try again."
    else:
        message = f"D8 didn't take the answer (HTTP {res.status_code}). Try again."
    return _not_sent(incident, message)


def _json(res):
    try:
        body = res.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _log_failure(incident, action, why):
    # Never the payload: it carries the person's email.
    current_app.logger.warning(
        "Lead %s for incident %s not sent to D8: %s", action, incident.id, why
    )


def _not_sent(incident, message):
    return Outcome(
        ok=False,
        stopped=False,
        message=message,
        incident_id=incident.id,
        http_status=502,
    )


def _http_url(value):
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return None


def _answered(incident, user, body):
    status = body["status"]
    try:
        oncall_service.resolve_incident(
            incident,
            user=user,
            announce=False,
            resolution={"reason": status, "by": user.email},
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # D8 has the answer and resolves the page itself; stopping it here
        # first is only so it stops sooner.
        current_app.logger.exception("resolving answered lead incident %s", incident.id)
    return Outcome(
        ok=True,
        stopped=True,
        message=ANSWER_MESSAGES[status],
        incident_id=incident.id,
        status=status,
        lead_url=_http_url(body.get("lead_url")),
    )


def _refused(incident, user, body, default_message):
    d8_message = body.get("message")
    if isinstance(d8_message, str) and 0 < len(d8_message.strip()) <= MAX_D8_MESSAGE:
        message = d8_message.strip()
    else:
        message = default_message
    try:
        oncall_service.acknowledge_incident(incident, user, announce=False)
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception("stopping refused lead incident %s", incident.id)
    return Outcome(
        ok=False,
        stopped=True,
        message=message,
        incident_id=incident.id,
        code=body["error"],
    )


def already_closed(incident):
    """The ``Outcome`` for an answer to a lead page that has already stopped
    (D8 resolved it): nothing is sent."""
    note = oncall_service.resolution_note(incident)
    message = (
        f"This offer has already closed ({note})."
        if note
        else ("This offer has already closed.")
    )
    return Outcome(
        ok=False,
        stopped=True,
        message=message,
        incident_id=incident.id,
        code="offer_closed",
    )
