import os
import time
import logging

# ── Constants ─────────────────────────────────────────────────────────────────

# Job/Operation log limits
OPERATION_LOG_MAX_ENTRIES = 200
JOB_MAX_ENTRIES = 100
JOB_EVENTS_MAX_ENTRIES = 100

# Status strings for image check results
STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_REGISTRY_ERROR = "registry_error"
STATUS_NOT_PULLED = "not_pulled"
STATUS_UNKNOWN = "unknown"

# Default timeout values (in seconds)
DEFAULT_COMPOSE_TIMEOUT = 300     # 5 minutes - for docker compose up/down
DEFAULT_PRUNE_TIMEOUT = 600       # 10 minutes - for docker prune operations
DEFAULT_REGISTRY_TIMEOUT = 15     # 15 seconds - for registry API calls
DEFAULT_PROXY_TIMEOUT = 15         # 15 seconds - for remote instance proxy requests


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


# Registry API rate limiting (in seconds)
REGISTRY_DELAY_SECONDS = get_int_env("REGISTRY_DELAY_SECONDS", 0)

# Notification throttling (in seconds, 0 = disabled)
NOTIFY_MAX_FREQUENCY = get_int_env("NOTIFY_MAX_FREQUENCY", 0)

# Notification batching settings
NOTIFY_BATCH_WINDOW = get_int_env("NOTIFY_BATCH_WINDOW", 300)  # 5 minutes default batch window
NOTIFY_SUMMARY_ENABLED = get_bool_env("NOTIFY_SUMMARY_ENABLED", True)  # Send summary notifications by default


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
NOTIFY_ON_BULK_COMPLETE = get_bool_env("NOTIFY_ON_BULK_COMPLETE", False)  # Reduced from True to minimize noise

REMOTE_INSTANCES_CONFIG = get_env("REMOTE_INSTANCES", "").strip()
REMOTE_INSTANCES_FILE = get_env("REMOTE_INSTANCES_FILE", "").strip()
TOKEN_CACHE_TTL = get_int_env("TOKEN_CACHE_TTL", 900)
REGISTRY_TOKEN_CACHE: dict[str, dict[str, object]] = {}


def cleanup_token_cache() -> int:
    """Remove expired tokens from the registry token cache.
    
    Returns:
        Number of tokens removed from the cache.
    """
    now = time.time()
    expired_keys = [
        key for key, value in REGISTRY_TOKEN_CACHE.items()
        if value.get("expires_at", 0) < now
    ]
    for key in expired_keys:
        REGISTRY_TOKEN_CACHE.pop(key, None)
    return len(expired_keys)


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(message)s"
)
