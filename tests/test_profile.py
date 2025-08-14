# tests/test_profile.py

from app.models import User


def test_update_address_success(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit valid data to the update_address endpoint
    THEN their address information should be updated in the database.
    """
    response = logged_in_client.put(
        "/profile/address",
        data={"country": "Canada", "city": "Toronto", "timezone": "EST"},
    )
    assert response.status_code == 200
    assert b"profile-header-card" in response.data
    assert b"Toronto" in response.data

    user = User.get_by_id(1)
    assert user.country == "Canada"
    assert user.city == "Toronto"
    assert user.timezone == "EST"


def test_update_presence_status_success(logged_in_client):
    """
    GIVEN a logged-in user with 'online' status
    WHEN they submit a valid new status ('away')
    THEN their status should be updated in the database.
    """
    user = User.get_by_id(1)
    assert user.presence_status == "online"

    response = logged_in_client.put("/profile/status", data={"status": "away"})
    assert response.status_code == 200
    assert b"profile-header-card" in response.data

    # Re-fetch the user from the database to get the updated values.
    updated_user = User.get_by_id(1)
    assert updated_user.presence_status == "away"


def test_update_presence_status_invalid(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit an invalid status
    THEN they should get a 400 error and their status should not change.
    """
    user = User.get_by_id(1)
    assert user.presence_status == "online"

    response = logged_in_client.put(
        "/profile/status", data={"status": "invalid-status"}
    )
    assert response.status_code == 400
    assert b"Invalid status" in response.data

    # Re-fetch the user to verify their status did not change.
    updated_user = User.get_by_id(1)
    assert updated_user.presence_status == "online"


def test_update_theme_success(logged_in_client):
    """
    GIVEN a logged-in user with the 'system' theme
    WHEN they submit a valid new theme ('dark')
    THEN their theme should be updated and they should receive an HX-Refresh header.
    """
    user = User.get_by_id(1)
    assert user.theme == "system"

    response = logged_in_client.put("/profile/theme", data={"theme": "dark"})
    assert response.status_code == 200
    assert response.headers.get("HX-Refresh") == "true"

    # Re-fetch the user to verify the new theme was saved.
    updated_user = User.get_by_id(1)
    assert updated_user.theme == "dark"


def test_update_theme_invalid(logged_in_client):
    """
    GIVEN a logged-in user
    WHEN they submit an invalid theme
    THEN they should get a 400 error and their theme should not change.
    """
    user = User.get_by_id(1)
    assert user.theme == "system"

    response = logged_in_client.put("/profile/theme", data={"theme": "invalid-theme"})
    assert response.status_code == 400
    assert b"Invalid theme" in response.data

    # Re-fetch the user to verify their theme did not change.
    updated_user = User.get_by_id(1)
    assert updated_user.theme == "system"
    assert user.theme == "system"


def test_get_address_display_partial(logged_in_client):
    """
    WHEN a user's address display partial is requested
    THEN it should return the correct partial with the user's info.
    """
    # First, set some data on the user to check for
    user = User.get_by_id(1)
    user.city = "Testville"
    user.save()

    response = logged_in_client.get("/profile/address/view")
    assert response.status_code == 200
    assert b"Testville" in response.data
    assert b"form-label" in response.data  # Check for label, indicating display view


def test_get_address_form_partial(logged_in_client):
    """
    WHEN a user's address edit form is requested
    THEN it should return the form partial with the user's info pre-filled.
    """
    # First, set some data on the user to check for
    user = User.get_by_id(1)
    user.country = "Testland"
    user.save()

    response = logged_in_client.get("/profile/address/edit")
    assert response.status_code == 200

    # --- THIS IS THE FIX ---
    # We create the expected HTML string and the response HTML string,
    # removing newlines and extra spaces from both to make the comparison robust.
    expected_html = b'<input type="text" class="form-control" id="country" name="country" value="Testland">'
    # Replace newlines and carriage returns, then replace multiple spaces with a single space
    response_html_flat = (
        response.data.replace(b"\n", b"").replace(b"\r", b"").replace(b"  ", b"")
    )

    # The assertion is now much more reliable.
    assert expected_html in response_html_flat


def test_set_wysiwyg_preference(logged_in_client):
    """
    GIVEN a logged-in user with WYSIWYG disabled by default
    WHEN they send a request to enable it
    THEN their preference should be updated in the database.
    """
    # 1. Verify the initial state (default is False)
    user = User.get_by_id(1)
    assert user.wysiwyg_enabled is False

    # 2. Send the request to enable the feature
    response = logged_in_client.put(
        "/chat/user/preference/wysiwyg", data={"wysiwyg_enabled": "true"}
    )

    # Assert the response is successful (204 No Content)
    assert response.status_code == 204

    # 3. Verify the change was persisted in the database
    updated_user = User.get_by_id(1)
    assert updated_user.wysiwyg_enabled is True

    # 4. Now, test turning it back off
    response_off = logged_in_client.put(
        "/chat/user/preference/wysiwyg", data={"wysiwyg_enabled": "false"}
    )
    assert response_off.status_code == 204
    user_turned_off = User.get_by_id(1)
    assert user_turned_off.wysiwyg_enabled is False
