# tests/test_chat.py

import pytest
from app.models import Channel, ChannelMember, User, WorkspaceMember

@pytest.fixture
def setup_channel_and_users(test_db):
    """
    Sets up a channel with one member (the default testuser) and a second user
    who is a member of the workspace but not the channel.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username='anotheruser', email='another@example.com')
    # Both users need to be in the workspace to be eligible for channel membership
    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)

    channel = Channel.create(workspace=workspace, name='team-channel')
    ChannelMember.create(user=user1, channel=channel)

    return {'user1': user1, 'user2': user2, 'channel': channel}

def test_get_create_channel_form(logged_in_client):
    """
    WHEN a logged-in user requests the create channel form
    THEN check they get the form partial with a 200 OK response.
    """
    # The form is loaded into a modal, so we simulate that GET request
    response = logged_in_client.get('/chat/channels/create')
    assert response.status_code == 200
    assert b'Create a New Channel' in response.data

def test_create_new_public_channel(logged_in_client):
    """
    WHEN a logged-in user posts valid data to create a public channel
    THEN check the channel is created and the user is a member.
    """
    response = logged_in_client.post('/chat/channels/create', data={
        'name': 'general-test-channel'
    })
    
    # Check for a successful response with the HTMX trigger
    assert response.status_code == 200
    assert response.headers['HX-Trigger'] == 'close-modal'
    
    # Verify the channel exists in the database
    channel = Channel.get_or_none(name='general-test-channel')
    assert channel is not None
    assert channel.is_private is False
    
    # Verify the creator is a member
    test_user = User.get_by_id(1)
    member = ChannelMember.get_or_none(user=test_user, channel=channel)
    assert member is not None

def test_access_channel_as_member(logged_in_client):
    """
    GIVEN a channel that the user is a member of
    WHEN the user requests the channel chat
    THEN check for a 200 OK response.
    """
    # First, create the channel and membership for the test setup
    channel = Channel.create(workspace_id=1, name='member-channel')
    test_user = User.get_by_id(1)
    ChannelMember.create(user=test_user, channel=channel)
    
    # Now, test the route
    response = logged_in_client.get(f'/chat/channel/{channel.id}')
    assert response.status_code == 200
    assert f'Welcome to #member-channel'.encode() in response.data

def test_access_channel_as_non_member(logged_in_client):
    """
    GIVEN a channel that the user is NOT a member of
    WHEN the user requests the channel chat
    THEN check for a 403 Forbidden response.
    """
    # Create a channel but DO NOT add the user as a member
    channel = Channel.create(workspace_id=1, name='secret-channel')
    
    # Test the route
    response = logged_in_client.get(f'/chat/channel/{channel.id}')
    assert response.status_code == 403
    assert b'Not a member of this channel' in response.data

def test_add_channel_member_success(logged_in_client, setup_channel_and_users):
    """
    GIVEN a channel member (user1)
    WHEN they add another workspace member (user2) to the channel
    THEN user2 should become a member of the channel.
    """
    channel = setup_channel_and_users['channel']
    user2 = setup_channel_and_users['user2']

    # Verify user2 is not yet a member
    assert ChannelMember.get_or_none(user=user2, channel=channel) is None

    response = logged_in_client.post(
        f'/chat/channel/{channel.id}/members',
        data={'user_id': user2.id}
    )

    assert response.status_code == 200
    # Verify user2 is now a member in the database
    assert ChannelMember.get_or_none(user=user2, channel=channel) is not None

def test_create_duplicate_channel_fails(logged_in_client):
    """
    GIVEN a channel with a specific name already exists
    WHEN a user tries to create a new channel with the same name
    THEN they should receive a 409 Conflict error.
    """
    # First, create the channel successfully
    channel_name = 'duplicate-test'
    response1 = logged_in_client.post('/chat/channels/create', data={'name': channel_name})
    assert response1.status_code == 200

    # Now, try to create it again
    response2 = logged_in_client.post('/chat/channels/create', data={'name': channel_name})
    assert response2.status_code == 409 # 409 Conflict

    # We check for the key phrases from the error message.
    assert b'channel named' in response2.data
    assert b'already exists' in response2.data

def test_create_invalid_channel_name_fails(logged_in_client):
    """
    WHEN a user tries to create a channel with an invalid name (too short)
    THEN they should receive a 400 Bad Request error.
    """
    response = logged_in_client.post('/chat/channels/create', data={'name': 'a'})
    assert response.status_code == 400
    assert b"Name must be at least 3 characters long" in response.data

def test_create_channel_sanitizes_name(logged_in_client):
    """
    WHEN a user tries to create a channel with special characters and uppercase letters
    THEN the channel should be created with a sanitized, lowercase name.
    """
    response = logged_in_client.post('/chat/channels/create', data={'name': 'Project-Alpha!!'})
    assert response.status_code == 200

    # Verify the channel was created with the sanitized name in the database
    sanitized_name = 'project-alpha'
    assert Channel.get_or_none(name=sanitized_name) is not None
