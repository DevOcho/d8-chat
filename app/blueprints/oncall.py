# app/blueprints/oncall.py
"""Web (session-auth) endpoints for the on-call pager.

The pager itself is client-side (``static/js/pager.js``): it rings on
``page`` WebSocket frames and calls these endpoints to acknowledge, resolve,
accept or reject a lead (``lead_response``), and, after every (re)connect,
fetch the pages it may have missed, since pub/sub is at-most-once. The mobile
equivalents live under ``/api/v1/oncall/*``.
"""

import json

from flask import Blueprint, g, jsonify, make_response, request

from app import limiter
from app.models import Incident, IncidentPage
from app.routes import login_required
from app.services import lead_response, oncall_service

oncall_bp = Blueprint("oncall", __name__)


def _error(message, status):
    """Plain text for HTMX callers (shown in the error toast), JSON otherwise."""
    if "HX-Request" in request.headers:
        return make_response(message, status)
    return jsonify({"error": message}), status


def _incident_or_404(incident_id):
    incident = Incident.get_or_none(Incident.id == incident_id)
    if incident is None:
        return None, _error("Incident not found.", 404)
    if not oncall_service.user_in_chain(incident, g.user):
        return None, _error("You are not in this incident's escalation chain.", 403)
    return incident, None


@oncall_bp.route("/oncall/pages/active")
@login_required
def active_pages():
    """Incidents currently paging the logged-in user."""
    return jsonify(
        {
            "incidents": [
                oncall_service.serialize_incident(i)
                for i in oncall_service.active_pages_for(g.user)
            ]
        }
    )


@oncall_bp.route("/oncall/incidents/<int:incident_id>/ack", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def acknowledge_incident(incident_id):
    """Acknowledge an incident: stops paging and escalation for everyone."""
    incident, error = _incident_or_404(incident_id)
    if error:
        return error
    if not oncall_service.acknowledge_incident(incident, g.user):
        # Someone else got there first — not an error worth a red toast.
        incident = Incident.get_by_id(incident.id)
        return jsonify(
            {"ok": False, "incident": oncall_service.serialize_incident(incident)}
        )
    incident = Incident.get_by_id(incident.id)
    return jsonify(
        {"ok": True, "incident": oncall_service.serialize_incident(incident)}
    )


@oncall_bp.route("/oncall/incidents/<int:incident_id>/resolve", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def resolve_incident(incident_id):
    """Resolve an incident by hand."""
    incident, error = _incident_or_404(incident_id)
    if error:
        return error
    resolved = oncall_service.resolve_incident(incident, user=g.user)
    incident = resolved or Incident.get_by_id(incident.id)
    return jsonify(
        {
            "ok": resolved is not None,
            "incident": oncall_service.serialize_incident(incident),
        }
    )


@oncall_bp.route(
    "/oncall/incidents/<int:incident_id>/<any(accept, reject):action>",
    methods=["POST"],
)
@login_required
@limiter.limit("30 per minute")
def answer_lead(incident_id, action):
    """Accept or reject the lead offer a D8 lead page rings for: D8-Chat sends
    the answer to D8 (``lead_response.respond``). Only a page D8 sent as an
    offer (``page.offer``) can be answered (400 otherwise, before calling D8),
    and only by someone it was sent to.

    JSON answer: ``200`` with the ``Outcome`` (``ok``, ``stopped``,
    ``message``, ``status``, ``lead_url``, ...) whenever the page is settled
    (answered, or nothing left to answer), else an ``{"error"}`` and the page
    keeps ringing (``502`` D8 never took it). HTMX callers (the incident card)
    get the ``Outcome`` as the ``lead-response`` event (``HX-Trigger``), which
    pager.js shows.
    """
    incident = Incident.get_or_none(Incident.id == incident_id)
    if incident is None:
        return _error("Incident not found.", 404)
    if not oncall_service.is_lead_offer(incident):
        return _error("Only a lead D8 offered can be accepted or rejected.", 400)
    if not oncall_service.lead_response_enabled(incident):
        return _error(
            "Accepting leads in D8-Chat isn't set up. Open the lead in D8.", 503
        )
    paged = (
        IncidentPage.select()
        .where((IncidentPage.incident == incident) & (IncidentPage.user == g.user))
        .exists()
    )
    if not paged:
        return _error(
            "Only the person this lead was offered to can accept or reject it.", 403
        )
    if incident.state == "resolved":
        outcome = lead_response.already_closed(incident)
    else:
        outcome = lead_response.respond(incident, g.user, action)
    if outcome.http_status != 200:
        return _error(outcome.message, outcome.http_status)
    if "HX-Request" in request.headers:
        response = make_response("", 200)
        response.headers["HX-Trigger"] = json.dumps(
            {"lead-response": outcome.to_dict()}
        )
        return response
    return jsonify(outcome.to_dict())


@oncall_bp.route("/oncall/test-page", methods=["POST"])
@login_required
@limiter.limit("6 per minute")
def test_page():
    """Ring the logged-in user's own pager so they can check it works."""
    oncall_service.send_test_page(g.user)
    return "", 204
