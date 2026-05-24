import os
import time
import logging

def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def get_bool_env(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_int_env(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ── Config ────────────────────────────────────────────────────────────────────
COMPOSE_ROOT = get_env("COMPOSE_ROOT", "/compose")
CHECK_INTERVAL_MINUTES = get_int_env("CHECK_INTERVAL_MINUTES", 60)
LOG_LEVEL = get_env("LOG_LEVEL", "INFO")
AUTO_RECREATE_AFTER_PULL = get_bool_env("AUTO_RECREATE_AFTER_PULL", False)
NOTIFY_ENABLED = get_bool_env("NOTIFY_ENABLED", False)
NOTIFY_BACKEND = get_env("NOTIFY_BACKEND", "").strip().lower()

NOTIFY_WEBHOOK_URL = get_env("NOTIFY_WEBHOOK_URL", "").strip()
NOTIFY_WEBHOOK_METHOD = get_env("NOTIFY_WEBHOOK_METHOD", "POST").strip().upper()
NOTIFY_WEBHOOK_TIMEOUT = get_int_env("NOTIFY_WEBHOOK_TIMEOUT", 10)

NOTIFY_MQTT_HOST = get_env("NOTIFY_MQTT_HOST", "").strip()
NOTIFY_MQTT_PORT = get_int_env("NOTIFY_MQTT_PORT", 1883)
NOTIFY_MQTT_TOPIC = get_env("NOTIFY_MQTT_TOPIC", "").strip()
NOTIFY_MQTT_USERNAME = get_env("NOTIFY_MQTT_USERNAME", "").strip()
NOTIFY_MQTT_PASSWORD = get_env("NOTIFY_MQTT_PASSWORD", "").strip()
NOTIFY_MQTT_RETAIN = get_bool_env("NOTIFY_MQTT_RETAIN", False)

NOTIFY_EMAIL_HOST = get_env("NOTIFY_EMAIL_HOST", "").strip()
NOTIFY_EMAIL_PORT = get_int_env("NOTIFY_EMAIL_PORT", 587)
NOTIFY_EMAIL_USERNAME = get_env("NOTIFY_EMAIL_USERNAME", "").strip()
NOTIFY_EMAIL_PASSWORD = get_env("NOTIFY_EMAIL_PASSWORD", "").strip()
NOTIFY_EMAIL_FROM = get_env("NOTIFY_EMAIL_FROM", "").strip()
NOTIFY_EMAIL_TO = get_env("NOTIFY_EMAIL_TO", "").strip()
NOTIFY_EMAIL_USE_TLS = get_bool_env("NOTIFY_EMAIL_USE_TLS", True)

NOTIFY_ON_UPDATES_FOUND = get_bool_env("NOTIFY_ON_UPDATES_FOUND", True)
NOTIFY_ON_PULL_SUCCESS = get_bool_env("NOTIFY_ON_PULL_SUCCESS", False)
NOTIFY_ON_PULL_ERROR = get_bool_env("NOTIFY_ON_PULL_ERROR", True)
NOTIFY_ON_RECREATE_SUCCESS = get_bool_env("NOTIFY_ON_RECREATE_SUCCESS", False)
NOTIFY_ON_RECREATE_ERROR = get_bool_env("NOTIFY_ON_RECREATE_ERROR", True)
NOTIFY_ON_BULK_COMPLETE = get_bool_env("NOTIFY_ON_BULK_COMPLETE", True)

REMOTE_INSTANCES_CONFIG = get_env("REMOTE_INSTANCES", "").strip()
REMOTE_INSTANCES_FILE = get_env("REMOTE_INSTANCES_FILE", "").strip()
TOKEN_CACHE_TTL = get_int_env("TOKEN_CACHE_TTL", 900)
REGISTRY_TOKEN_CACHE: dict[str, dict[str, object]] = {}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(message)s"
)
