"""Tests for config.py environment parsing and helpers."""
import time

import config


def test_get_bool_env_truthy_values(monkeypatch):
    for value in ("true", "TRUE", "1", "yes", "on", "Yes"):
        monkeypatch.setenv("TEST_FLAG", value)
        assert config.get_bool_env("TEST_FLAG") is True, value


def test_get_bool_env_falsy_values(monkeypatch):
    for value in ("false", "0", "no", "off", "", "garbage"):
        monkeypatch.setenv("TEST_FLAG", value)
        assert config.get_bool_env("TEST_FLAG") is False, value


def test_get_bool_env_default_when_unset(monkeypatch):
    monkeypatch.delenv("TEST_FLAG", raising=False)
    assert config.get_bool_env("TEST_FLAG") is False
    assert config.get_bool_env("TEST_FLAG", default=True) is True


def test_get_int_env_valid(monkeypatch):
    monkeypatch.setenv("TEST_NUM", "42")
    assert config.get_int_env("TEST_NUM") == 42


def test_get_int_env_invalid_uses_default(monkeypatch):
    monkeypatch.setenv("TEST_NUM", "not-a-number")
    assert config.get_int_env("TEST_NUM", default=7) == 7


def test_get_int_env_unset_uses_default(monkeypatch):
    monkeypatch.delenv("TEST_NUM", raising=False)
    assert config.get_int_env("TEST_NUM", default=15) == 15


def test_get_env_strips_nothing_by_default(monkeypatch):
    monkeypatch.setenv("TEST_RAW", "value")
    assert config.get_env("TEST_RAW") == "value"
    assert config.get_env("TEST_MISSING", default="fallback") == "fallback"


def test_status_constants_are_distinct():
    values = {
        config.STATUS_UP_TO_DATE,
        config.STATUS_UPDATE_AVAILABLE,
        config.STATUS_REGISTRY_ERROR,
        config.STATUS_NOT_PULLED,
        config.STATUS_UNKNOWN,
    }
    assert values == {
        "up_to_date",
        "update_available",
        "registry_error",
        "not_pulled",
        "unknown",
    }


def test_cleanup_token_cache_removes_only_expired_entries():
    config.REGISTRY_TOKEN_CACHE.clear()
    config.REGISTRY_TOKEN_CACHE["expired"] = {"token": "old", "expires_at": time.time() - 10}
    config.REGISTRY_TOKEN_CACHE["fresh"] = {"token": "new", "expires_at": time.time() + 999}

    removed = config.cleanup_token_cache()

    assert removed == 1
    assert "expired" not in config.REGISTRY_TOKEN_CACHE
    assert "fresh" in config.REGISTRY_TOKEN_CACHE
    config.REGISTRY_TOKEN_CACHE.clear()
