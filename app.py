"""docker-update-checker entry point.

Creates the Flask application (services.py), registers the HTTP routes
(api.py), validates startup configuration, and — when run as a script —
starts the background workers and the web server.
"""
import logging
import sys
from pathlib import Path

from config import COMPOSE_ROOT
from services import app, start_background_workers

# Importing api registers all Flask routes on the app object.
import api  # noqa: F401  (side effect: route registration)

log = logging.getLogger(__name__)

# ── Startup Validation ────────────────────────────────────────────────────────
# Validate COMPOSE_ROOT exists and is readable
compose_root_path = Path(COMPOSE_ROOT)
if not compose_root_path.exists():
    log.warning(f"COMPOSE_ROOT '{COMPOSE_ROOT}' does not exist. No compose files will be found.")
elif not compose_root_path.is_dir():
    log.error(f"COMPOSE_ROOT '{COMPOSE_ROOT}' is not a directory.")
    sys.exit(1)
else:
    log.info(f"COMPOSE_ROOT set to: {COMPOSE_ROOT}")


if __name__ == "__main__":
    start_background_workers()
    app.run(host="0.0.0.0", port=5000, debug=False)
