# tests/conftest.py

import os

os.environ["PYTEST_CURRENT_TEST"] = "true"

import pytest
from app import create_app
from app.models import (
    db,
    User,
    Workspace,
    WorkspaceMember,
    Channel,
    ChannelMember,
    Conversation,
    Message,
    UserConversationStatus,
    Mention,
    Reaction,
)


@pytest.fixture(scope="session")
def app():
    """
    Creates a single Flask app instance for the entire test session.
    """
    app = create_app(config_class="config.TestConfig")
    return app


@pytest.fixture(scope="function")
def client(app):
    """
    Creates a new test client for each test function. This ensures that
    things like the session are clean for every test.
    """
    with app.test_client() as client:
        yield client


@pytest.fixture(scope="function", autouse=True)
def test_db(app):
    """
    This special fixture automatically runs for every test function.
    It initializes the database, creates all tables, seeds essential data,
    and then cleans everything up after the test is done.
    """
    with app.app_context():
        # A list of all your models
        tables = [
            User,
            Workspace,
            WorkspaceMember,
            Conversation,
            Channel,
            ChannelMember,
            Message,
            UserConversationStatus,
            Mention,
            Reaction,
        ]

        db.create_tables(tables)

        # Seed the database with one essential user and workspace
        workspace, _ = Workspace.get_or_create(name="Test Workspace")
        user, _ = User.get_or_create(
            id=1,
            username="testuser",
            email="test@example.com",
            display_name="Test User",
        )
        WorkspaceMember.get_or_create(user=user, workspace=workspace)

        yield  # The test runs at this point

        # Teardown: drop all tables to ensure isolation
        db.drop_tables(tables)


@pytest.fixture(scope="function")
def logged_in_client(client):
    """
    A fixture that provides a test client which is already logged in
    as the default test user (id=1).
    """
    with client.session_transaction() as sess:
        sess["user_id"] = 1
    yield client
