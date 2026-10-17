# tests/test_config.py

import pytest

from config import s3_setting


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("S3_ENDPOINT", "MINIO_ENDPOINT", "S3_ACCESS_KEY", "MINIO_ROOT_USER"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_s3_names_are_ignored_until_s3_endpoint_is_set(clean_env):
    """Garage's key sits in the Secret before the cutover. Until the configmap
    adds S3_ENDPOINT the app must keep using MinIO's credentials, or a restart
    would send Garage's key to MinIO."""
    clean_env.setenv("MINIO_ENDPOINT", "d8-chat-minio:9000")
    clean_env.setenv("MINIO_ROOT_USER", "minio-root")
    clean_env.setenv("S3_ACCESS_KEY", "GK0123456789abcdef01234567")
    assert s3_setting("S3_ENDPOINT", "MINIO_ENDPOINT") == "d8-chat-minio:9000"
    assert s3_setting("S3_ACCESS_KEY", "MINIO_ROOT_USER") == "minio-root"


def test_s3_endpoint_switches_to_the_s3_names(clean_env):
    clean_env.setenv("MINIO_ENDPOINT", "d8-chat-minio:9000")
    clean_env.setenv("MINIO_ROOT_USER", "minio-root")
    clean_env.setenv("S3_ENDPOINT", "d8-chat-garage:3900")
    clean_env.setenv("S3_ACCESS_KEY", "GK0123456789abcdef01234567")
    assert s3_setting("S3_ENDPOINT", "MINIO_ENDPOINT") == "d8-chat-garage:3900"
    assert (
        s3_setting("S3_ACCESS_KEY", "MINIO_ROOT_USER") == "GK0123456789abcdef01234567"
    )


def test_after_the_switch_a_missing_s3_name_falls_back(clean_env):
    clean_env.setenv("S3_ENDPOINT", "d8-chat-garage:3900")
    clean_env.setenv("S3_ACCESS_KEY", "")
    clean_env.setenv("MINIO_ROOT_USER", "minio-root")
    assert s3_setting("S3_ACCESS_KEY", "MINIO_ROOT_USER") == "minio-root"


def test_default_when_nothing_is_set(clean_env):
    assert s3_setting("S3_ACCESS_KEY", "MINIO_ROOT_USER") is None
    assert (
        s3_setting("S3_ENDPOINT", "MINIO_ENDPOINT", "localhost:9000")
        == "localhost:9000"
    )
