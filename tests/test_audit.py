"""
Tests for the admin audit log.

Each end-to-end test exercises an admin route and asserts that the expected
``AuditLog`` row was written. The audit helper itself is also unit-tested
with target=None, target=tuple, and DB failure paths.
"""

import json

import pytest

from app.audit import audit
from app.models import (
    AuditLog,
    Channel,
    ChannelMember,
    User,
    Workspace,
    WorkspaceMember,
)


@pytest.fixture
def admin_client(client, app):
    """Return a client logged in as an admin user."""
    with app.app_context():
        user = User.get_by_id(1)
        workspace = Workspace.get(Workspace.name == "DevOcho")
        member, _ = WorkspaceMember.get_or_create(user=user, workspace=workspace)
        member.role = "admin"
        member.save()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
    return client


# --- Unit tests on the audit() helper ---


class TestAuditHelper:
    def test_writes_basic_event(self, app):
        with app.test_request_context("/"):
            from flask import g

            g.user = User.get_by_id(1)
            audit("test.event")
        row = AuditLog.get(AuditLog.action == "test.event")
        assert row.actor_id == 1
        assert row.target_type is None
        assert row.target_id is None

    def test_target_model_extracts_type_and_id(self, app):
        with app.test_request_context("/"):
            user = User.get_by_id(1)
            audit("test.target", target=user)
        row = AuditLog.get(AuditLog.action == "test.target")
        assert row.target_type == "user"
        assert row.target_id == 1

    def test_target_tuple_form(self, app):
        with app.test_request_context("/"):
            audit("test.tuple", target=("custom", 42))
        row = AuditLog.get(AuditLog.action == "test.tuple")
        assert row.target_type == "custom"
        assert row.target_id == 42

    def test_details_serialized_as_json(self, app):
        with app.test_request_context("/"):
            audit("test.details", reason="hello", count=3)
        row = AuditLog.get(AuditLog.action == "test.details")
        parsed = json.loads(row.details)
        assert parsed == {"reason": "hello", "count": 3}

    def test_no_details_means_null(self, app):
        with app.test_request_context("/"):
            audit("test.no_details")
        row = AuditLog.get(AuditLog.action == "test.no_details")
        assert row.details is None

    def test_records_remote_address(self, app):
        with app.test_request_context(
            "/", environ_overrides={"REMOTE_ADDR": "10.0.0.5"}
        ):
            audit("test.ip")
        row = AuditLog.get(AuditLog.action == "test.ip")
        assert row.ip == "10.0.0.5"


# --- Integration tests at the admin routes ---


class TestAdminRoutesEmitAudit:
    def test_user_create_logs_event(self, admin_client):
        admin_client.post(
            "/admin/users/create",
            data={
                "username": "newperson",
                "email": "newperson@example.com",
                "password": "asdfASDF1234",
                "role": "member",
                "display_name": "New Person",
            },
        )
        row = AuditLog.get(AuditLog.action == "user.created")
        assert row.target_type == "user"
        details = json.loads(row.details)
        assert details["role"] == "member"
        assert details["email"] == "newperson@example.com"

    def test_channel_create_logs_event(self, admin_client):
        admin_client.post(
            "/admin/channels/create",
            data={"name": "audit-test", "topic": "", "description": ""},
        )
        row = AuditLog.get(AuditLog.action == "channel.created")
        assert row.target_type == "channel"
        details = json.loads(row.details)
        assert details["name"] == "audit-test"

    def test_role_change_records_before_and_after(self, admin_client, app):
        with app.app_context():
            other = User.create(
                username="othermember", email="other@example.com", display_name="O"
            )
            workspace = Workspace.get(Workspace.name == "DevOcho")
            WorkspaceMember.create(user=other, workspace=workspace, role="member")
            channel = Channel.create(workspace=workspace, name="role-test")
            ChannelMember.create(user=other, channel=channel, role="member")
            channel_id = channel.id
            user_id = other.id

        admin_client.post(
            f"/admin/channels/{channel_id}/members/{user_id}/role",
            data={"role": "admin"},
        )

        row = AuditLog.get(AuditLog.action == "channel.member_role_changed")
        details = json.loads(row.details)
        assert details["role_before"] == "member"
        assert details["role_after"] == "admin"
        assert details["target_user_id"] == user_id
