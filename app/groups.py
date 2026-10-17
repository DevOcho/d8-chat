"""User groups: IdP-synced and admin-managed group membership.

Groups let an on-call escalation level target a role ("whoever is in
``director``") instead of a hard-coded user, so a reorg is an Authentik change
rather than a d8-chat config change.

Two sources feed ``UserGroup``:

* ``sso`` — mirrored from the OIDC ``groups`` claim on every SSO login.
  Authentik's default ``profile`` scope mapping includes it. Membership is only
  as fresh as the user's last SSO login.
* ``local`` — set by an admin in the user editor. Never touched by SSO sync,
  so password-login deployments and local dev can use groups too.
"""

from .models import User, UserGroup


def normalize_group_name(name):
    """Stored/compared form of a group name: stripped and lowercased."""
    return (name or "").strip().lower()


def users_in_group(name):
    """Active users who belong to group ``name`` (any source)."""
    normalized = normalize_group_name(name)
    if not normalized:
        return []
    return list(
        User.select()
        .join(UserGroup)
        .where((UserGroup.name == normalized) & (User.is_active))
        .order_by(User.username)
        .distinct()
    )


def group_names_for(user):
    """Sorted list of ``(name, source)`` tuples for ``user``."""
    return [
        (row.name, row.source)
        for row in UserGroup.select()
        .where(UserGroup.user == user)
        .order_by(UserGroup.name)
    ]


def sync_sso_groups(user, groups):
    """Make ``user``'s SSO-sourced groups match the IdP's ``groups`` claim.

    ``groups`` of ``None`` means the claim was absent (scope mapping missing,
    or the IdP doesn't send it) — leave memberships alone rather than wiping
    them. An empty list is a real answer and removes every SSO group.
    Local groups are never modified; an SSO group that duplicates a local one
    is skipped (the local row already grants membership).
    """
    if groups is None:
        return
    if isinstance(groups, str):
        groups = [groups]
    wanted = {
        normalize_group_name(g) for g in groups if isinstance(g, str) and g.strip()
    }
    existing = {
        row.name: row for row in UserGroup.select().where(UserGroup.user == user)
    }

    for name, row in existing.items():
        if row.source == "sso" and name not in wanted:
            row.delete_instance()
    for name in wanted - set(existing):
        UserGroup.create(user=user, name=name, source="sso")


def set_local_groups(user, names):
    """Replace ``user``'s admin-managed (local) groups with ``names``.

    A name that already exists as an SSO group is converted to local so the
    admin's choice survives the next SSO sync.
    """
    wanted = {normalize_group_name(n) for n in names if normalize_group_name(n)}
    existing = {
        row.name: row for row in UserGroup.select().where(UserGroup.user == user)
    }

    for name, row in existing.items():
        if row.source == "local" and name not in wanted:
            row.delete_instance()
    for name in wanted:
        row = existing.get(name)
        if row is None:
            UserGroup.create(user=user, name=name, source="local")
        elif row.source != "local":
            row.source = "local"
            row.save()
