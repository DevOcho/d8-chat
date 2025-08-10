# tests/test_profile.py

from app.models import User

def test_update_address_success(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit valid data to the update_address endpoint
    THEN their address information should be updated in the database.
    """
    response = logged_in_client.put('/profile/address', data={
        'country': 'Canada',
        'city': 'Toronto',
        'timezone': 'EST'
    })
    assert response.status_code == 200
    assert b'profile-header-card' in response.data
    assert b'Toronto' in response.data

    user = User.get_by_id(1)
    assert user.country == 'Canada'
    assert user.city == 'Toronto'
    assert user.timezone == 'EST'

def test_update_presence_status_success(logged_in_client):
    """
    GIVEN a logged-in user with 'online' status
    WHEN they submit a valid new status ('away')
    THEN their status should be updated in the database.
    """
    user = User.get_by_id(1)
    assert user.presence_status == 'online'

    response = logged_in_client.put('/profile/status', data={'status': 'away'})
    assert response.status_code == 200
    assert b'profile-header-card' in response.data

    # Re-fetch the user from the database to get the updated values.
    updated_user = User.get_by_id(1)
    assert updated_user.presence_status == 'away'

def test_update_presence_status_invalid(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit an invalid status
    THEN they should get a 400 error and their status should not change.
    """
    user = User.get_by_id(1)
    assert user.presence_status == 'online'

    response = logged_in_client.put('/profile/status', data={'status': 'invalid-status'})
    assert response.status_code == 400
    assert b'Invalid status' in response.data

    # Re-fetch the user to verify their status did not change.
    updated_user = User.get_by_id(1)
    assert updated_user.presence_status == 'online'

def test_update_theme_success(logged_in_client):
    """
    GIVEN a logged-in user with the 'system' theme
    WHEN they submit a valid new theme ('dark')
    THEN their theme should be updated and they should receive an HX-Refresh header.
    """
    user = User.get_by_id(1)
    assert user.theme == 'system'

    response = logged_in_client.put('/profile/theme', data={'theme': 'dark'})
    assert response.status_code == 200
    assert response.headers.get('HX-Refresh') == 'true'

    # Re-fetch the user to verify the new theme was saved.
    updated_user = User.get_by_id(1)
    assert updated_user.theme == 'dark'

def test_update_theme_invalid(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit an invalid theme
    THEN they should get a 400 error and their theme should not change.
    """
    user = User.get_by_id(1)
    assert user.theme == 'system'

    response = logged_in_client.put('/profile/theme', data={'theme': 'invalid-theme'})
    assert response.status_code == 400
    assert b'Invalid theme' in response.data

    # Re-fetch the user to verify their theme did not change.
    updated_user = User.get_by_id(1)
    assert updated_user.theme == 'system'
    assert user.theme == 'system'
