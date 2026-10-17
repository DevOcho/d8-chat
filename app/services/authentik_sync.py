"""Mirror Authentik account status (and groups) into d8-chat once a minute.

d8-chat otherwise only hears from Authentik at sign-in, so someone deactivated
or deleted there would keep their web session and their phone's 30-day API
token. Each pass lists Authentik's users and:

* deactivates (``account_service.deactivate_user``) every SSO-linked d8-chat
  user whose Authentik account is inactive or gone: sessions, API tokens,
  pushes and open sockets all stop;
* with AUTHENTIK_SYNC_GROUPS, mirrors each active user's Authentik groups into
  their ``sso`` groups, so escalation groups and ``oncall-admin`` changes apply
  without a re-login.

It never reactivates anyone. Bringing someone back stays a d8-chat admin
action (reactivate them in Authentik first, or the next pass undoes it), and
an Authentik change can't undo a deactivation made in d8-chat.

Matching: ``User.sso_id`` is the OIDC ``sub``; AUTHENTIK_SUBJECT_FIELD names
the Authentik user field it equals (``uid`` for the default subject mode).

Safety: a pass acts only on a complete listing. If none of the SSO users can
be found it changes nothing (a wrong subject field, or a token that can't see
users). If more are missing than MISSING_LIMIT / MISSING_SHARE allow, the
missing ones are left alone and an error is logged; accounts Authentik
explicitly reports as inactive are still deactivated.

One pass runs per interval across all workers and pods (a Valkey lock).
Only enabled when AUTHENTIK_API_TOKEN is set (see ``create_app``).
"""

import time
from urllib.parse import urlsplit

import requests
from flask import current_app

from app.audit import audit
from app.chat_manager import chat_manager
from app.groups import sync_sso_groups
from app.models import User, db
from app.services import account_service

SUBJECT_FIELDS = ("uid", "username", "email", "pk", "uuid")
PAGE_SIZE = 100
MAX_PAGES = 1000
REQUEST_TIMEOUT = 15
# More missing SSO users than this (and than this share of them) in one pass
# looks like a listing problem, not a wave of departures.
MISSING_LIMIT = 3
MISSING_SHARE = 0.10
LOCK_KEY = "authentik-sync:lock"


class SyncError(Exception):
    """The listing can't be trusted; the pass changed nothing."""


def api_base_url():
    """AUTHENTIK_API_URL, or the origin of OIDC_ISSUER_URL."""
    cfg = current_app.config
    base = cfg.get("AUTHENTIK_API_URL")
    if not base:
        issuer = urlsplit(cfg.get("OIDC_ISSUER_URL") or "")
        if issuer.scheme and issuer.netloc:
            base = f"{issuer.scheme}://{issuer.netloc}"
    return (base or "").rstrip("/")


def fetch_users(include_groups):
    """Every Authentik user (all pages), or SyncError."""
    base = api_base_url()
    if not base:
        raise SyncError("set AUTHENTIK_API_URL or OIDC_ISSUER_URL")
    headers = {
        "Authorization": f"Bearer {current_app.config['AUTHENTIK_API_TOKEN']}",
        "Accept": "application/json",
    }
    users = []
    page = 1
    for _ in range(MAX_PAGES):
        try:
            response = requests.get(
                f"{base}/api/v3/core/users/",
                headers=headers,
                params={
                    "page": page,
                    "page_size": PAGE_SIZE,
                    "include_groups": "true" if include_groups else "false",
                    "include_roles": "false",
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SyncError(f"listing users (page {page}) failed: {exc}") from exc
        users.extend(body.get("results") or [])
        next_page = int((body.get("pagination") or {}).get("next") or 0)
        if next_page <= page:
            return users
        page = next_page
    raise SyncError(f"more than {MAX_PAGES} pages of users")


def _remote_groups(remote_user):
    """Group names, or None if the listing didn't include groups."""
    groups = remote_user.get("groups_obj")
    if groups is None:
        return None
    return [g["name"] for g in groups if isinstance(g, dict) and g.get("name")]


def sync_once():
    """One pass. Returns a summary dict; raises SyncError without changes."""
    cfg = current_app.config
    field = cfg.get("AUTHENTIK_SUBJECT_FIELD") or "uid"
    if field not in SUBJECT_FIELDS:
        raise SyncError(f"AUTHENTIK_SUBJECT_FIELD must be one of {SUBJECT_FIELDS}")
    sync_groups = bool(cfg.get("AUTHENTIK_SYNC_GROUPS"))

    remote = {}
    for remote_user in fetch_users(include_groups=sync_groups):
        key = remote_user.get(field)
        if key not in (None, ""):
            remote[str(key)] = remote_user

    linked = list(User.select().where(User.sso_id.is_null(False) & User.is_active))
    missing = [user for user in linked if user.sso_id not in remote]
    if linked and len(missing) == len(linked):
        raise SyncError(
            f"none of the {len(linked)} SSO users were found by {field!r}; check "
            "AUTHENTIK_SUBJECT_FIELD and that the token can view users"
        )
    hold_missing = len(missing) > max(MISSING_LIMIT, int(len(linked) * MISSING_SHARE))
    if hold_missing:
        current_app.logger.error(
            "Authentik sync: %d of %d SSO users are missing from Authentik; not "
            "deactivating them (deactivate by hand if they really left): %s",
            len(missing),
            len(linked),
            ", ".join(sorted(user.username for user in missing)),
        )

    deactivated = []
    for user in linked:
        remote_user = remote.get(user.sso_id)
        if remote_user is None:
            if hold_missing:
                continue
            reason = "not found in Authentik"
        elif not remote_user.get("is_active", True):
            reason = "deactivated in Authentik"
        else:
            if sync_groups:
                sync_sso_groups(user, _remote_groups(remote_user))
            continue
        if account_service.deactivate_user(user):
            audit("user.deactivated", target=user, source="authentik", reason=reason)
            current_app.logger.warning(
                "Authentik sync: deactivated %s (%s)", user.username, reason
            )
            deactivated.append(user.username)

    return {
        "authentik_users": len(remote),
        "sso_users": len(linked),
        "deactivated": deactivated,
        "held_missing": len(missing) if hold_missing else 0,
    }


def _claim_pass(interval):
    """True for the one worker that gets this interval's pass."""
    try:
        return bool(
            chat_manager.redis_client.set(
                LOCK_KEY, "1", nx=True, ex=max(interval - 5, 10)
            )
        )
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.warning("Authentik sync: Valkey unavailable; skipping")
        return False


def run_sync_loop(app):
    """Forever: one pass per interval across the cluster."""
    interval = max(15, int(app.config.get("AUTHENTIK_SYNC_INTERVAL_SECONDS") or 60))
    while True:
        time.sleep(interval)
        with app.app_context():
            try:
                if _claim_pass(interval):
                    sync_once()
            except SyncError as exc:
                app.logger.error("Authentik sync skipped: %s", exc)
            except Exception:  # pylint: disable=broad-exception-caught
                app.logger.exception("Authentik sync failed")
            finally:
                try:
                    if not db.is_closed():
                        db.close()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
