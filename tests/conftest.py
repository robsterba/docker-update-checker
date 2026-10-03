"""Shared test configuration for docker-update-checker.

config.py reads environment variables at import time, so they must be set
before any project module is imported. pytest imports this conftest before
test modules, which makes it the right place to pin the test environment.
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Deterministic, isolated environment for the whole test session
os.environ["COMPOSE_ROOT"] = str(PROJECT_ROOT / "tests" / "fixtures" / "compose")
os.environ["APP_SETTINGS_FILE"] = str(Path(tempfile.gettempdir()) / "duc-test-app-settings.json")
os.environ["NOTIFICATION_SETTINGS_FILE"] = str(Path(tempfile.gettempdir()) / "duc-test-notification-settings.json")
os.environ["NOTIFY_ENABLED"] = "false"
os.environ["NOTIFY_BACKEND"] = ""
os.environ["NOTIFY_BATCH_WINDOW"] = "0"
os.environ["REMOTE_INSTANCES"] = ""
os.environ["REMOTE_INSTANCES_FILE"] = ""
os.environ["CHECK_INTERVAL_MINUTES"] = "60"
os.environ["AUTO_RECREATE_AFTER_PULL"] = "false"
os.environ["SELF_UPDATE_CHECK_ENABLED"] = "false"
os.environ["OS_UPDATE_CHECK_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

# Remove stale settings files from previous runs so tests start clean
for path in (os.environ["APP_SETTINGS_FILE"], os.environ["NOTIFICATION_SETTINGS_FILE"]):
    Path(path).unlink(missing_ok=True)

import pytest  # noqa: E402


@pytest.fixture
def client():
    """Flask test client with all routes registered."""
    import api  # noqa: F401  (side effect: route registration)
    from services import app

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def clean_state():
    """Reset shared job/check state around a test."""
    import jobs

    jobs.check_results.clear()
    jobs.jobs_state.clear()
    jobs.operations_log._entries.clear()
    yield
    jobs.check_results.clear()
    jobs.jobs_state.clear()
    jobs.operations_log._entries.clear()


@pytest.fixture(autouse=True)
def clear_token_cache():
    """Keep the registry token cache from leaking between tests."""
    from config import REGISTRY_TOKEN_CACHE

    REGISTRY_TOKEN_CACHE.clear()
    yield
    REGISTRY_TOKEN_CACHE.clear()
