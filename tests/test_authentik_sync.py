# tests/test_authentik_sync.py
"""The Authentik account sync (app/services/authentik_sync.py): people
deactivated or deleted in Authentik lose d8-chat access within a pass, and
their groups follow Authentik, without ever locking everyone out."""

import json
from unittest.mock import Mock

import pytest
import requests

from app.groups import group_names_for, set_local_groups
from app.models import AuditLog, DeviceToken, User, Workspace, WorkspaceMember
from app.services import authentik_sync


def _sso_user(username, sso_id, groups=()):
    user = User.create(
        username=username,
        email=f"{username}@example.com",
        display_name=username.title(),
        sso_id=sso_id,
    )
    WorkspaceMember.create(user=user, workspace=Workspace.get(name="DevOcho"))
    set_local_groups(user, groups)
    return user


def _remote(uid, *, active=True, groups=(), username=None):
    return {
        "pk": abs(hash(uid)) % 10_000,
        "uid": uid,
        "username": username or f"ak-{uid}",
        "email": f"{uid}@example.com",
        "is_active": active,
        "groups_obj": [{"pk": f"g-{g}", "name": g} for g in groups],
    }


def _response(results, next_page=0):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "pagination": {"next": next_page},
        "results": results,
    }
    return response


@pytest.fixture
def authentik(app, mocker, monkeypatch):
    """Sync enabled; ``serve(*pages)`` sets what Authentik's listing returns."""
    for key, value in {
        "AUTHENTIK_API_TOKEN": "test-token",
        "AUTHENTIK_API_URL": None,
        "OIDC_ISSUER_URL": "https://auth.example.com/application/o/d8-chat/",
        "AUTHENTIK_SUBJECT_FIELD": "uid",
        "AUTHENTIK_SYNC_GROUPS": True,
    }.items():
        monkeypatch.setitem(app.config, key, value)
    get = mocker.patch("app.services.authentik_sync.requests.get")
    disconnect = mocker.patch(
        "app.services.account_service.chat_manager.disconnect_user"
    )

    def serve(*pages):
        get.side_effect = [
            _response(page, next_page=i + 2 if i + 1 < len(pages) else 0)
            for i, page in enumerate(pages)
        ]

    get.serve = serve
    get.disconnect = disconnect
    return get


def _sync(app):
    with app.app_context():
        return authentik_sync.sync_once()


def test_deactivated_in_authentik_loses_access(app, authentik):
    leaver = _sso_user("leaver", "uid-leaver")
    stayer = _sso_user("stayer", "uid-stayer")
    DeviceToken.create(user=leaver, token="fcm-leaver", platform="android")
    authentik.serve([_remote("uid-leaver", active=False), _remote("uid-stayer")])

    summary = _sync(app)

    assert summary["deactivated"] == ["leaver"]
    assert User.get_by_id(leaver.id).is_active is False
    assert User.get_active_by_id(leaver.id) is None  # sessions + API tokens refused
    assert not DeviceToken.select().where(DeviceToken.user == leaver).exists()
    authentik.disconnect.assert_called_once_with(leaver.id, "Account deactivated")
    assert User.get_by_id(stayer.id).is_active is True

    row = AuditLog.get(AuditLog.action == "user.deactivated")
    assert row.target_id == leaver.id and row.actor is None
    assert json.loads(row.details) == {
        "source": "authentik",
        "reason": "deactivated in Authentik",
    }


def test_deleted_from_authentik_loses_access(app, authentik):
    gone = _sso_user("gone", "uid-gone")
    others = [_sso_user(f"user{i}", f"uid-{i}") for i in range(5)]
    authentik.serve([_remote(f"uid-{i}") for i in range(5)])

    assert _sync(app)["deactivated"] == ["gone"]
    assert User.get_by_id(gone.id).is_active is False
    assert all(User.get_by_id(u.id).is_active for u in others)


def test_users_without_sso_are_left_alone(app, authentik):
    local = User.create(username="local", email="local@example.com")
    _sso_user("sso", "uid-sso")
    authentik.serve([_remote("uid-sso")])

    _sync(app)
    assert User.get_by_id(local.id).is_active is True


def test_never_reactivates(app, authentik):
    """Bringing someone back stays a deliberate d8-chat admin action."""
    benched = _sso_user("benched", "uid-benched")
    User.update(is_active=False).where(User.id == benched.id).execute()
    _sso_user("other", "uid-other")
    authentik.serve([_remote("uid-benched"), _remote("uid-other")])

    _sync(app)
    assert User.get_by_id(benched.id).is_active is False


def test_groups_follow_authentik_and_local_groups_stay(app, authentik):
    ops = _sso_user("ops", "uid-ops", groups=["local-only"])
    authentik.serve([_remote("uid-ops", groups=["oncall-admin", "DevOps-Lead"])])

    _sync(app)
    assert group_names_for(ops) == [
        ("devops-lead", "sso"),
        ("local-only", "local"),
        ("oncall-admin", "sso"),
    ]

    authentik.serve([_remote("uid-ops", groups=["devops-lead"])])
    _sync(app)
    assert group_names_for(ops) == [("devops-lead", "sso"), ("local-only", "local")]


def test_group_sync_can_be_turned_off(app, authentik, monkeypatch):
    monkeypatch.setitem(app.config, "AUTHENTIK_SYNC_GROUPS", False)
    ops = _sso_user("ops", "uid-ops")
    authentik.serve([_remote("uid-ops", groups=["oncall-admin"])])

    _sync(app)
    assert group_names_for(ops) == []
    assert authentik.call_args.kwargs["params"]["include_groups"] == "false"


def test_follows_every_page(app, authentik):
    _sso_user("first", "uid-first")
    late = _sso_user("late", "uid-late")
    authentik.serve([_remote("uid-first")], [_remote("uid-late", active=False)])

    assert _sync(app)["deactivated"] == ["late"]
    assert [c.kwargs["params"]["page"] for c in authentik.call_args_list] == [1, 2]
    assert User.get_by_id(late.id).is_active is False


def test_calls_the_issuer_origin_with_the_token(app, authentik):
    _sso_user("one", "uid-one")
    authentik.serve([_remote("uid-one")])
    _sync(app)
    call = authentik.call_args
    assert call.args[0] == "https://auth.example.com/api/v3/core/users/"
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_matches_on_the_configured_subject_field(app, authentik, monkeypatch):
    monkeypatch.setitem(app.config, "AUTHENTIK_SUBJECT_FIELD", "username")
    leaver = _sso_user("leaver", "leaver-username")
    _sso_user("stayer", "stayer-username")
    authentik.serve(
        [
            _remote("x1", username="leaver-username", active=False),
            _remote("x2", username="stayer-username"),
        ]
    )
    assert _sync(app)["deactivated"] == ["leaver"]
    assert User.get_by_id(leaver.id).is_active is False


def test_a_failed_page_changes_nothing(app, authentik):
    _sso_user("first", "uid-first")
    late = _sso_user("late", "uid-late")
    authentik.side_effect = [
        _response([_remote("uid-first")], next_page=2),
        requests.ConnectionError("authentik down"),
    ]
    with pytest.raises(authentik_sync.SyncError):
        _sync(app)
    assert User.get_by_id(late.id).is_active is True


def test_nobody_found_changes_nothing(app, authentik):
    """A wrong subject field or a token that can't see users must not lock
    everyone out."""
    users = [_sso_user(f"user{i}", f"uid-{i}") for i in range(3)]
    authentik.serve([_remote("someone-else")])
    with pytest.raises(authentik_sync.SyncError, match="none of the 3"):
        _sync(app)
    assert all(User.get_by_id(u.id).is_active for u in users)


def test_too_many_missing_are_held_but_explicit_deactivations_still_apply(
    app, authentik
):
    present = [_sso_user(f"here{i}", f"uid-here{i}") for i in range(3)]
    missing = [_sso_user(f"miss{i}", f"uid-miss{i}") for i in range(4)]
    leaver = _sso_user("leaver", "uid-leaver")
    authentik.serve(
        [_remote(f"uid-here{i}") for i in range(3)]
        + [_remote("uid-leaver", active=False)]
    )

    summary = _sync(app)
    assert summary["deactivated"] == ["leaver"]
    assert summary["held_missing"] == 4
    assert User.get_by_id(leaver.id).is_active is False
    assert all(User.get_by_id(u.id).is_active for u in present + missing)


def test_rejects_an_unknown_subject_field(app, authentik, monkeypatch):
    monkeypatch.setitem(app.config, "AUTHENTIK_SUBJECT_FIELD", "name")
    _sso_user("one", "uid-one")
    with pytest.raises(authentik_sync.SyncError, match="AUTHENTIK_SUBJECT_FIELD"):
        _sync(app)
    authentik.assert_not_called()


def test_one_pass_per_interval_across_workers(app, mocker):
    redis = mocker.patch("app.services.authentik_sync.chat_manager.redis_client")
    redis.set.side_effect = [True, None]
    with app.app_context():
        assert authentik_sync._claim_pass(60) is True
        assert authentik_sync._claim_pass(60) is False
    redis.set.assert_called_with("authentik-sync:lock", "1", nx=True, ex=55)
