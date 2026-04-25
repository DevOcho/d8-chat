"""
Signed tokens for self-service authentication flows (password reset, etc.).

We sign with the app's ``SECRET_KEY`` via ``itsdangerous`` and include the
target user id plus a fingerprint of their current password hash. Two
properties fall out:

  * **Time-limited.** ``URLSafeTimedSerializer.loads(max_age=...)`` rejects
    tokens older than the configured TTL.
  * **Single-use after reset.** Once the user changes their password, the
    hash fingerprint embedded in any outstanding token no longer matches the
    one in the DB, so old tokens stop working — no token table needed.

If multiple reset emails are outstanding at once (e.g. user spammed the
forgot-password form), all of them remain valid until one succeeds; that's
the standard tradeoff for a stateless reset.
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .models import User

PASSWORD_RESET_SALT = "password-reset"
PASSWORD_RESET_TTL_SECONDS = 30 * 60  # 30 minutes


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key)


def _hash_fingerprint(user: User) -> str:
    """Stable per-(user, current-password) string. Empty hash → empty fingerprint."""
    return (user.password_hash or "")[:16]


def make_password_reset_token(secret_key: str, user: User) -> str:
    """Issue a fresh reset token for ``user``."""
    return _serializer(secret_key).dumps(
        {"user_id": user.id, "fp": _hash_fingerprint(user)},
        salt=PASSWORD_RESET_SALT,
    )


def verify_password_reset_token(secret_key: str, token: str) -> User | None:
    """
    Decode and validate a reset token. Returns the matching ``User`` on
    success, ``None`` on any failure (expired, tampered, password already
    changed, user no longer active).
    """
    try:
        data = _serializer(secret_key).loads(
            token, salt=PASSWORD_RESET_SALT, max_age=PASSWORD_RESET_TTL_SECONDS
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None

    user_id = data.get("user_id")
    fp = data.get("fp")
    if not user_id or fp is None:
        return None

    user = User.get_active_by_id(user_id)
    if user is None:
        return None
    if _hash_fingerprint(user) != fp:
        # Password already changed since the token was issued — token is dead.
        return None
    return user
