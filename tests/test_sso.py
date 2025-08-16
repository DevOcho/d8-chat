# tests/test_sso.py

from app.models import User, WorkspaceMember, ChannelMember, Channel
from flask import request


def test_sso_callback_creates_new_user(client, mocker):
    """
    GIVEN a new user authenticating via SSO for the first time
    WHEN the SSO provider redirects them back to our /auth callback
    THEN a new User, WorkspaceMember, and ChannelMember records should be created.
    """
    # 1. Define the fake data we expect back from the SSO provider
    fake_user_info = {
        'sub': 'fake_sso_id_123',
        'email': 'new.user@example.com',
        'given_name': 'Newbie'
    }

    # 2. Mock the external Authlib calls
    mocker.patch('app.sso.oauth.authentik.authorize_access_token', return_value={'access_token': 'fake_token'})
    mocker.patch('app.sso.oauth.authentik.parse_id_token', return_value=fake_user_info)

    # 3. Set up the session, since our app expects a 'nonce' to be present
    with client.session_transaction() as sess:
        sess['nonce'] = 'test_nonce'

    # 4. Make the request to our callback endpoint
    response = client.get('/auth', follow_redirects=True)

    # --- 5. Assert the results ---

    # Assert we were redirected to the profile page, indicating a successful login
    assert response.status_code == 200
    assert response.request.path == '/profile'

    # Assert a new user was created in the database with the correct details
    new_user = User.get_or_none(User.sso_id == 'fake_sso_id_123')
    assert new_user is not None
    assert new_user.email == 'new.user@example.com'
    assert new_user.display_name == 'Newbie'
    assert new_user.username == 'new_user'

    # Assert the user was added to the default workspace
    workspace_member = WorkspaceMember.get_or_none(user=new_user)
    assert workspace_member is not None
    assert workspace_member.role == 'member'

    # Assert the user was added to the 'general' and 'announcements' channels
    general_channel = Channel.get(Channel.name == 'general')
    announcements_channel = Channel.get(Channel.name == 'announcements')
    
    assert ChannelMember.get_or_none(user=new_user, channel=general_channel) is not None
    assert ChannelMember.get_or_none(user=new_user, channel=announcements_channel) is not None
