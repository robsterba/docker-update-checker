"""
API token authentication.

When API_TOKEN is set, every /api/* request must carry an Authorization:
Bearer <token> header. Requests without a valid token receive a 401, and
repeated failures from the same client address are locked out temporarily.

The dashboard shell ("/", "/favicon.ico", "/health") and "/api/version" stay
open so the UI can load and prompt for the token. When API_TOKEN is unset the
app runs open, exactly as before.
"""
import hmac
import logging
import threading
import time
from collections import defaultdict

from flask import jsonify, request

import config

log = logging.getLogger(__name__)

# API paths that never require a token: the version endpoint feeds the login
# prompt. Non-/api paths (the dashboard shell, favicon, /health) are not
# guarded at all.
EXEMPT_API_PATHS = {"/api/version"}

MAX_FAILURES = 10          # failed attempts before lockout
FAILURE_WINDOW_SECONDS = 60  # window in which failures accumulate
LOCKOUT_SECONDS = 60       # lockout duration
LOCKOUT_LOG_EVERY = 10     # seconds between lockout log lines (log hygiene)

_failures: dict[str, list[float]] = defaultdict(list)
_lockout_logged: dict[str, float] = {}
_lock = threading.Lock()


def clear_failures() -> None:
    """Reset all recorded auth failures (used by tests)."""
    with _lock:
        _failures.clear()
        _lockout_logged.clear()


def _client_key() -> str:
    return (request.remote_addr or "unknown").strip() or "unknown"


def _log_path(path: str) -> str:
    """Request paths are attacker-controlled: printable ASCII, truncated."""
    printable = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in path)
    return printable[:80]


def check_token() -> bool:
    """Timing-safe comparison of the Authorization header with API_TOKEN."""
    expected = f"Bearer {config.API_TOKEN}"
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _denied(message: str, status: int):
    response = jsonify({"status": "error", "message": message})
    response.headers["Cache-Control"] = "no-store"
    return response, status


def api_auth_guard():
    """Flask before_request guard for the API surface."""
    if not config.API_TOKEN:
        return None  # authentication not configured; app stays open

    path = request.path
    if not path.startswith("/api"):
        return None  # only the API surface is protected
    if path in EXEMPT_API_PATHS:
        return None

    key = _client_key()
    now = time.monotonic()
    with _lock:
        recent = [t for t in _failures[key] if now - t < FAILURE_WINDOW_SECONDS]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        if len(recent) >= MAX_FAILURES:
            # log at most once per LOCKOUT_LOG_EVERY seconds to keep a
            # hammering client from flooding the log
            last_logged = _lockout_logged.get(key, 0.0)
            if now - last_logged >= LOCKOUT_LOG_EVERY:
                _lockout_logged[key] = now
                log.warning(f"API auth lockout active for {key}")
            return _denied("Too many failed attempts; try again later", 429)

    if check_token():
        with _lock:
            _failures.pop(key, None)
            _lockout_logged.pop(key, None)
        return None

    with _lock:
        _failures[key].append(now)
    log.warning(f"API auth failed for {key} on {_log_path(path)}")
    return _denied("Unauthorized", 401)
