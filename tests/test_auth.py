# tests/test_auth.py

from flask import session
from app.models import User

def test_login_redirect(client):
    """
    GIVEN a Flask application configured for testing
    WHEN the '/chat' page is requested (GET) and the user is not logged in
    THEN check that the response is a redirect to the login page
    """
    response = client.get('/chat', follow_redirects=False)
    assert response.status_code == 302
    assert '/login' in response.headers['Location']

def test_profile_access(logged_in_client):
    """
    WHEN the '/profile' page is requested by a logged-in user
    THEN check for a 200 OK response and that user's data is present.
    """
    response = logged_in_client.get('/profile')
    assert response.status_code == 200
    assert b'Test User' in response.data

def test_logout(logged_in_client):
    """
    WHEN the '/logout' route is requested by a logged-in user
    THEN check that they are logged out and redirected.
    """
    # Hit the logout endpoint
    response = logged_in_client.get('/logout', follow_redirects=True)
    assert response.status_code == 200  # Should land on the index page

    # Now verify that a protected route requires login again
    response = logged_in_client.get('/chat', follow_redirects=False)
    assert response.status_code == 302
    assert '/login' in response.headers['Location']
