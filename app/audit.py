"""
Admin audit logging.

Single helper, ``audit(action, target=..., **details)``, that writes a row to
the ``AuditLog`` table. Intended for security-relevant admin actions: user
create/edit/deactivate, role changes, channel mutations, etc. Failures are
logged-and-swallowed — an audit miss should never break the action it was
trying to record (degrade open rather than fail open).

For more on what to record: actor identity comes from ``g.user`` (or
``g.api_user``); the actor's IP is taken from ``request.remote_addr``; the
target is either a model instance (we extract type+id automatically) or a
``(type_name, id)`` tuple for things that aren't a Peewee row.
"""

import json

from flask import current_app, g, has_request_context, request

from .models import AuditLog


def audit(action: str, target=None, **details) -> None:
    """Record an admin action. See module docstring for conventions."""
    target_type: str | None = None
    target_id: int | None = None
    if target is not None:
        if isinstance(target, tuple) and len(target) == 2:
            target_type, target_id = target[0], int(target[1])
        elif hasattr(target, "id"):
            target_type = type(target).__name__.lower()
            target_id = target.id

    actor = None
    ip = None
    if has_request_context():
        actor = getattr(g, "user", None) or getattr(g, "api_user", None)
        ip = request.remote_addr

    try:
        AuditLog.create(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=json.dumps(details) if details else None,
            ip=ip,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # An audit miss must not break the request. Log it and move on.
        current_app.logger.exception(f"Failed to record audit event {action!r}")
