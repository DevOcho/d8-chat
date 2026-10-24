# tests/test_login_rate_limit.py
"""The password-login rate limits come from config (LOGIN_RATE_LIMIT,
LOGIN_USERNAME_RATE_LIMIT), so the local cluster can loosen them for e2e
runs while every other environment keeps the defaults."""

import pytest

from app import create_app, limiter


@pytest.fixture
def limited_app(app):
    app.config["RATELIMIT_ENABLED"] = True
    app.config["RATELIMIT_STORAGE_URI"] = "memory://"
    limiter.init_app(app)
    limiter.reset()
    yield app
    app.config["RATELIMIT_ENABLED"] = False
    app.config["LOGIN_RATE_LIMIT"] = "5 per minute; 50 per hour"
    app.config["LOGIN_USERNAME_RATE_LIMIT"] = "10 per minute; 50 per hour"
    limiter.reset()


def _statuses(client, n, path="/login", username="nobody"):
    statuses = []
    for _ in range(n):
        if path == "/login":
            res = client.post(path, data={"username": username, "password": "x"})
        else:
            res = client.post(path, json={"username": username, "password": "x"})
        statuses.append(res.status_code)
    return statuses


def test_defaults_are_the_production_limits():
    from config import Config

    assert Config.LOGIN_RATE_LIMIT == "5 per minute; 50 per hour"
    assert Config.LOGIN_USERNAME_RATE_LIMIT == "10 per minute; 50 per hour"


def test_the_default_per_ip_limit_still_applies(limited_app):
    with limited_app.test_client() as client:
        statuses = _statuses(client, 6)
    assert statuses[:5].count(429) == 0
    assert statuses[5] == 429


@pytest.mark.parametrize("path", ["/login", "/api/v1/auth/login"])
def test_the_per_ip_limit_follows_config(limited_app, path):
    limited_app.config["LOGIN_RATE_LIMIT"] = "2 per minute"
    with limited_app.test_client() as client:
        statuses = _statuses(client, 3, path)
    assert 429 not in statuses[:2]
    assert statuses[2] == 429


def test_the_per_username_limit_follows_config(limited_app):
    limited_app.config["LOGIN_RATE_LIMIT"] = "100 per minute"
    limited_app.config["LOGIN_USERNAME_RATE_LIMIT"] = "1 per minute"
    with limited_app.test_client() as client:
        assert _statuses(client, 2, username="robert")[1] == 429
        # Another account isn't affected.
        assert _statuses(client, 1, username="emilio") != [429]


def test_a_looser_limit_lets_more_logins_through(limited_app):
    limited_app.config["LOGIN_RATE_LIMIT"] = "60 per minute; 1000 per hour"
    limited_app.config["LOGIN_USERNAME_RATE_LIMIT"] = "60 per minute; 1000 per hour"
    with limited_app.test_client() as client:
        assert 429 not in _statuses(client, 20)


def test_an_unparsable_limit_fails_at_boot(monkeypatch):
    from config import TestConfig

    monkeypatch.setattr(TestConfig, "LOGIN_RATE_LIMIT", "lots")
    with pytest.raises(RuntimeError, match="LOGIN_RATE_LIMIT"):
        create_app(config_class="config.TestConfig")
