"""0014_chief_of_staff_bot.py

The ``chief-of-staff`` bot that D8's Chief of Staff DMs come from (the
morning brief; D8 plan/CHIEF_OF_STAFF.md). POST /api/v1/internal/notify with
``to`` posts as it. Inactive, so it can never log in.

No schema change. Seeds (idempotent) the bot and its workspace membership.
"""

# pylint: disable=C0103

from app.models import Workspace
from app.services.chief_of_staff import ensure_bot


def _default_workspace():
    workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
    if workspace is None:
        workspace = Workspace.select().order_by(Workspace.id).first()
    return workspace


def migrate():
    """Seed the bot."""
    workspace = _default_workspace()
    if workspace is None:
        # Nothing to seed against: a fresh init_db.py run creates it all.
        return
    ensure_bot(workspace)


def rollback():
    """Leave the bot: its DMs are history until retention removes them."""
