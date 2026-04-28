"""Centralized password validation.

Single place for the project's password rules so handlers that accept a
password (self-service reset, admin create-user, admin password reset)
all run the same checks. Returns an error message string when the input
fails, or None when it's acceptable.
"""

MIN_PASSWORD_LENGTH = 12


def validate_password(password):
    """Return an error message if the password is unacceptable, else None.

    Rules applied:
      * Must be a non-empty string.
      * Must be at least MIN_PASSWORD_LENGTH characters.
      * Must contain at least one letter and one digit (to block trivially
        guessable values like "passwordpassword").
    """
    if not password or not isinstance(password, str):
        return "A password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        return "Password must contain at least one letter and one digit."
    return None
