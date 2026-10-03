"""
Native taskd integration.

Reconciles image check results into a taskd instance after each full scan:
one long-lived parent task per host ("Container updates: <host>") with one
subtask per outdated image. Existing subtasks are refreshed in place and
completed once their image is up to date again (or removed from all compose
files). The sync is best-effort: taskd being unreachable never fails a scan.

Settings come from TASKD_* environment defaults, optionally overridden at
runtime via taskd_settings.json (file wins, matching the notification
settings precedence).
"""
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

import config
from config import (
    STATUS_UPDATE_AVAILABLE,
    STATUS_UP_TO_DATE,
    TASKD_SETTINGS_FILE,
)
from jobs import log_op

log = logging.getLogger(__name__)

# Serializes syncs so overlapping scans never double-create tasks
_sync_lock = threading.Lock()

# Subtask titles are "Update <image>"; this prefix marks managed subtasks
SUBTASK_PREFIX = "Update "


def load_taskd_settings() -> dict:
    """Load taskd settings from file."""
    try:
        path = Path(TASKD_SETTINGS_FILE).expanduser()
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                log.info(f"Loaded taskd settings from {path}")
                return settings if isinstance(settings, dict) else {}
        return {}
    except Exception as e:
        log.warning(f"Unable to load taskd settings from {TASKD_SETTINGS_FILE}: {e}")
        return {}


def save_taskd_settings(settings: dict) -> bool:
    """Save taskd settings to file."""
    try:
        path = Path(TASKD_SETTINGS_FILE).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        log.info(f"Saved taskd settings to {path}")
        return True
    except Exception as e:
        log.error(f"Unable to save taskd settings to {TASKD_SETTINGS_FILE}: {e}")
        return False


def _normalize_tags(value: Any) -> list[str]:
    """Normalize a tags value (list or comma string) to lowercase names."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return list(config.TASKD_TAGS)
    return sorted({
        str(tag).strip().lower()
        for tag in items
        if str(tag).strip()
    })


def get_taskd_settings() -> dict:
    """Effective taskd settings: env defaults overridden by the settings file."""
    defaults = {
        "enabled": config.TASKD_ENABLED,
        "url": config.TASKD_URL,
        "timeout": config.TASKD_TIMEOUT,
        "tags": list(config.TASKD_TAGS),
        "source": config.TASKD_SOURCE,
        "host_label": config.TASKD_HOST_LABEL,
    }
    return normalize_taskd_settings({**defaults, **load_taskd_settings()})


def _normalize_url(value: Any) -> str:
    """Strip a URL and auto-prefix a scheme, matching remote instance handling."""
    url = str(value or "").strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def normalize_taskd_settings(settings: dict) -> dict:
    """Coerce arbitrary settings data into a clean, typed shape.

    Only the known keys are kept, so junk from a request body or a hand-edited
    settings file cannot leak into taskd_settings.json.
    """
    raw = settings or {}
    normalized = {
        "enabled": _normalize_bool(raw.get("enabled")),
        "url": _normalize_url(raw.get("url")),
        "timeout": raw.get("timeout"),
        "tags": _normalize_tags(raw.get("tags")),
        "source": str(raw.get("source") or "").strip() or "docker-update-checker",
        "host_label": str(raw.get("host_label") or "").strip(),
    }
    try:
        timeout = int(normalized["timeout"] or 0)
    except (TypeError, ValueError):
        timeout = 0
    if timeout <= 0:
        timeout = config.TASKD_TIMEOUT
    normalized["timeout"] = min(timeout, 60)
    return normalized


def _normalize_bool(value: Any) -> bool:
    """Strict boolean parsing: string flags use the get_bool_env truthy set."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def host_key(settings: dict) -> str:
    """Stable identity of this host in taskd."""
    return settings.get("host_label") or socket.gethostname()


# ── taskd REST client ────────────────────────────────────────────────────────


def _request(settings: dict, method: str, path: str, **kwargs) -> requests.Response:
    url = f"{settings['url']}/api/v1{path}"
    r = requests.request(method, url, timeout=settings["timeout"], **kwargs)
    r.raise_for_status()
    return r


def search_tasks(settings: dict, params: dict) -> list[dict]:
    """Query the task list; returns the task objects from the response."""
    r = _request(settings, "GET", "/tasks", params=params)
    return r.json().get("tasks", [])


def get_task(settings: dict, task_id: str) -> Optional[dict]:
    r = _request(settings, "GET", f"/tasks/{task_id}")
    return r.json()


def create_task(settings: dict, body: dict) -> dict:
    r = _request(settings, "POST", "/tasks", json=body)
    return r.json()


def patch_task(settings: dict, task_id: str, body: dict) -> dict:
    r = _request(settings, "PATCH", f"/tasks/{task_id}", json=body)
    return r.json()


def complete_task(settings: dict, task_id: str) -> dict:
    r = _request(settings, "POST", f"/tasks/{task_id}/complete")
    return r.json()


def test_taskd_connection(url: Optional[str] = None,
                          timeout: Optional[int] = None) -> dict:
    """Check reachability of a taskd instance.

    Uses the effective settings unless url/timeout overrides are given, so
    the UI can test before saving. Never raises.
    """
    settings = get_taskd_settings()
    probe = {
        "url": _normalize_url(url or settings["url"]),
        "timeout": timeout or settings["timeout"],
    }
    try:
        probe["timeout"] = min(max(int(probe["timeout"]), 1), 60)
    except (TypeError, ValueError):
        probe["timeout"] = settings["timeout"]
    if not probe["url"]:
        return {"status": "error", "message": "No taskd URL configured"}
    try:
        started = datetime.now(timezone.utc)
        r = requests.get(f"{probe['url']}/api/v1/health", timeout=probe["timeout"])
        r.raise_for_status()
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return {"status": "success", "latency_ms": latency_ms, "url": probe["url"]}
    except Exception as e:
        return {"status": "error", "message": str(e), "url": probe["url"]}


# ── Sync ─────────────────────────────────────────────────────────────────────


def _parent_title(settings: dict) -> str:
    return f"Container updates: {host_key(settings)}"


def _parent_description(settings: dict, outdated_count: int) -> str:
    # Keep this stable between scans when nothing changed: the description is
    # used for change detection, and PATCHing on every sync would spam taskd
    # webhook events and bump updated_at for no informational gain.
    return (
        f"Automated parent task managed by docker-update-checker. Do not rename.\n\n"
        f"Host: {host_key(settings)}\n"
        f"Outdated images: {outdated_count}"
    )


def _subtask_title(image: str) -> str:
    return f"{SUBTASK_PREFIX}{image}"


def _subtask_description(result: dict) -> str:
    # Deliberately excludes checked_at: the description doubles as change
    # detection, so it must only change when the tracked facts change.
    lines = [
        f"Image: {result.get('image', '')}",
        f"Local digest: {result.get('local_digest') or 'unknown'}",
        f"Remote digest: {result.get('remote_digest') or 'unknown'}",
    ]
    stacks = result.get("stacks") or []
    if stacks:
        lines.append(f"Stacks: {', '.join(stacks)}")
    compose_files = result.get("compose_files") or []
    if compose_files:
        lines.append(f"Compose files: {', '.join(compose_files)}")
    return "\n".join(lines)


def _find_parent(settings: dict) -> Optional[dict]:
    """Find this host's parent task by exact title match."""
    title = _parent_title(settings)
    tasks = search_tasks(settings, {"search": title, "parent": "none", "limit": 100})
    matches = [t for t in tasks if t.get("name") == title and t.get("status") != "archived"]
    if not matches:
        return None
    if len(matches) > 1:
        log.warning(f"Multiple taskd parent tasks named '{title}'; using the most recent")
    # Prefer a match that carries this host's marker, then the most recently updated
    marker = f"Host: {host_key(settings)}"
    marked = [t for t in matches if marker in (t.get("description") or "")]
    pool = marked or matches
    return max(pool, key=lambda t: t.get("updated_at") or "")


def _can_complete(result: Optional[dict]) -> bool:
    """Safe-to-complete rule: only clear evidence of freshness completes a task."""
    if result is None:
        return True  # image no longer in any compose file
    return result.get("status") == STATUS_UP_TO_DATE


def _sync_host_results(settings: dict, results: dict, allow_completion: bool) -> dict:
    """Reconcile one host's scan results into taskd. Raises on HTTP errors."""
    desired = {
        img: r for img, r in results.items()
        if r.get("status") == STATUS_UPDATE_AVAILABLE
    }

    parent = _find_parent(settings)
    if parent is None:
        parent = create_task(settings, {
            "name": _parent_title(settings),
            "description": _parent_description(settings, len(desired)),
            "tags": settings["tags"],
            "source": settings["source"],
        })
        log.info(f"Created taskd parent task {parent.get('id')}")
    else:
        wanted_description = _parent_description(settings, len(desired))
        if (parent.get("description") != wanted_description
                or parent.get("tags") != settings["tags"]):
            patch_task(settings, parent["id"], {
                "description": wanted_description,
                "tags": settings["tags"],
            })

    subtasks = {
        t.get("name"): t
        for t in (get_task(settings, parent["id"]).get("subtasks") or [])
        if t.get("status") != "archived"
    }

    created = patched = completed = 0

    for image, result in sorted(desired.items()):
        title = _subtask_title(image)
        existing = subtasks.get(title)
        if existing is None:
            create_task(settings, {
                "name": title,
                "description": _subtask_description(result),
                "parent_task_id": parent["id"],
                "tags": settings["tags"],
                "source": settings["source"],
            })
            created += 1
            continue
        wanted_description = _subtask_description(result)
        needs_refresh = (existing.get("description") != wanted_description
                         or existing.get("tags") != settings["tags"])
        needs_reopen = existing.get("status") == "done"
        if needs_refresh or needs_reopen:
            body = {"description": wanted_description, "tags": settings["tags"]}
            if needs_reopen:
                body["status"] = "todo"
            patch_task(settings, existing["id"], body)
            patched += 1

    for title, existing in sorted(subtasks.items()):
        if not allow_completion:
            break  # degenerate scan: no evidence anything was removed or updated
        if not title.startswith(SUBTASK_PREFIX):
            continue  # not managed by this integration
        if existing.get("source") != settings["source"]:
            continue  # user-created subtask; leave it alone
        if existing.get("status") == "done":
            continue
        image = title[len(SUBTASK_PREFIX):]
        if _can_complete(results.get(image)):
            complete_task(settings, existing["id"])
            completed += 1

    return {
        "parent_id": parent["id"],
        "outdated": len(desired),
        "created": created,
        "updated": patched,
        "completed": completed,
    }


def sync_results(results: dict, allow_completion: bool = True) -> dict:
    """Reconcile scan results into taskd. Never raises.

    Args:
        results: image check results keyed by image reference.
        allow_completion: pass False when the scan may be degenerate (e.g. no
            compose files found). With no scan data, an absent image is not
            evidence that it was removed, so the completion pass is skipped.

    Returns a summary dict with a "status" of:
      success | skipped (disabled or busy) | error (taskd unreachable)
    """
    settings = get_taskd_settings()
    if not settings["enabled"] or not settings["url"]:
        return {"status": "skipped", "reason": "disabled"}

    if not _sync_lock.acquire(blocking=False):
        log.info("taskd sync skipped: a previous sync is still in progress")
        return {"status": "skipped", "reason": "sync in progress"}
    try:
        summary = _sync_host_results(settings, results, allow_completion and bool(results))
        message = (
            f"Synced {summary['outdated']} outdated image(s) to taskd: "
            f"{summary['created']} created, {summary['updated']} updated, "
            f"{summary['completed']} completed"
        )
        log_op("taskd", "sync", "success", message)
        log.info(f"taskd sync complete: {message}")
        return {"status": "success", **summary, "message": message}
    except Exception as e:
        log.warning(f"taskd sync failed: {e}")
        log_op("taskd", "sync", "error", f"taskd sync failed: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        _sync_lock.release()
