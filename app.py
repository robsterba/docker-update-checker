import os
import sys
import json
import logging
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
import re
import requests
import docker
import yaml
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

# ── Import configuration from config.py ──────────────────────────────────────
from config import (
    COMPOSE_ROOT,
    CHECK_INTERVAL_MINUTES,
    LOG_LEVEL,
    AUTO_RECREATE_AFTER_PULL,
    NOTIFY_ENABLED,
    NOTIFY_BACKEND,
    REMOTE_INSTANCES_CONFIG,
    REMOTE_INSTANCES_FILE,
    TOKEN_CACHE_TTL,
    REGISTRY_TOKEN_CACHE,
    VERSION,
    NOTIFICATION_SETTINGS_FILE,
    # Status constants
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    STATUS_REGISTRY_ERROR,
    STATUS_NOT_PULLED,
    STATUS_UNKNOWN,
    # Timeout constants
    DEFAULT_COMPOSE_TIMEOUT,
    DEFAULT_PRUNE_TIMEOUT,
    DEFAULT_PROXY_TIMEOUT,
)

# ── Import from canonical modules ────────────────────────────────────────────
from jobs import (
    state_lock,
    check_results,
    operations_log,
    job_manager,
    jobs_state,
    log_op,
    create_job,
    update_job,
    finish_job,
    set_last_full_check,
    get_check_results,
    get_last_full_check,
    get_jobs_state,
)
from notifier import (
    send_notification,
    notify_updates_found,
    notify_pull_result,
    notify_recreate_result,
    notify_bulk_complete,
    build_notification_payload,
)

# ── Import Docker utilities from docker_utils.py ───────────────────────────────
from docker_utils import (
    docker_client,
    read_dotenv,
    resolve_env_vars,
    parse_images_from_compose,
    get_services_for_image,
    find_compose_files,
    list_compose_files_detailed,
    get_compose_file_content,
    write_compose_file,
    validate_compose_content,
    get_compose_file_dependencies,
    recreate_compose,
    parse_image_ref,
    get_registry_token,
    get_remote_digest,
    get_local_digest,
    check_image,
    get_host_resources,
    list_containers,
    get_container_resources,
    get_all_container_resources,
    inspect_container,
    start_container,
    stop_container,
    restart_container,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)

# Fix for circular imports - allows api.py to import from app
sys.modules["app"] = sys.modules[__name__]

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

# Note: docker_client is imported from docker_utils above
# Docker socket connection is initialized in docker_utils.py

# ── Helpers ───────────────────────────────────────────────────────────────────

def derive_stack_name(compose_path: str) -> str:
    p = Path(compose_path)
    return p.parent.name or "default"


def normalize_remote_instance(entry: dict | str) -> Optional[dict]:
    if isinstance(entry, str):
        if '|' in entry:
            name, url = entry.split('|', 1)
            entry = {"name": name.strip(), "url": url.strip()}
        else:
            entry = {"name": entry.strip(), "url": entry.strip()}

    if not isinstance(entry, dict):
        return None

    url = str(entry.get("url", "") or "").strip()
    if not url:
        return None
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"http://{url}"

    name = str(entry.get("name", "") or url).strip()
    description = str(entry.get("description", "") or "").strip()
    instance_id = str(entry.get("id", "") or name or url).strip()
    instance_id = re.sub(r'[^a-z0-9_-]+', '-', instance_id.lower()).strip('-')
    if not instance_id:
        return None

    return {
        "id": instance_id,
        "name": name,
        "url": url.rstrip('/'),
        "description": description,
        "type": "remote",
    }


def load_remote_instances() -> list[dict]:
    instances = []
    sources = []

    if REMOTE_INSTANCES_FILE:
        try:
            path = Path(REMOTE_INSTANCES_FILE).expanduser()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    sources.append(json.load(f))
                log.info(f"Loaded remote instances from {path}")
            else:
                log.warning(f"Remote instances file not found: {path}")
        except Exception as e:
            log.warning(f"Unable to load remote instances from {REMOTE_INSTANCES_FILE}: {e}")

    if REMOTE_INSTANCES_CONFIG:
        try:
            sources.append(json.loads(REMOTE_INSTANCES_CONFIG))
            log.info("Loaded remote instances from REMOTE_INSTANCES env var")
        except json.JSONDecodeError:
            instances_config = [line.strip() for line in REMOTE_INSTANCES_CONFIG.splitlines() if line.strip()]
            sources.append(instances_config)
            log.info(f"Loaded {len(instances_config)} remote instances from newline-delimited REMOTE_INSTANCES env var")
        except Exception as e:
            log.warning(f"Unable to parse REMOTE_INSTANCES: {e}")

    for source in sources:
        if isinstance(source, dict):
            source = [source]
        if not isinstance(source, list):
            continue
        for item in source:
            normalized = normalize_remote_instance(item)
            if normalized:
                instances.append(normalized)

    unique = {}
    for instance in instances:
        unique[instance["id"]] = instance
    log.info(f"Total remote instances loaded: {len(list(unique.values()))}")
    return list(unique.values())


def load_notification_settings() -> dict:
    """Load notification settings from file."""
    try:
        path = Path(NOTIFICATION_SETTINGS_FILE).expanduser()
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                log.info(f"Loaded notification settings from {path}")
                return settings
        else:
            log.info(f"Notification settings file not found: {path}, using defaults")
            return {}
    except Exception as e:
        log.warning(f"Unable to load notification settings from {NOTIFICATION_SETTINGS_FILE}: {e}")
        return {}


def save_notification_settings(settings: dict) -> bool:
    """Save notification settings to file."""
    try:
        path = Path(NOTIFICATION_SETTINGS_FILE).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        log.info(f"Saved notification settings to {path}")
        return True
    except Exception as e:
        log.error(f"Unable to save notification settings to {NOTIFICATION_SETTINGS_FILE}: {e}")
        return False


def get_all_instances() -> list[dict]:
    instances = [{
        "id": "local",
        "name": "Local host",
        "url": "",
        "description": "The Docker host running this service",
        "type": "local",
    }]
    instances.extend(load_remote_instances())
    return instances


def get_instance(instance_id: str) -> Optional[dict]:
    if instance_id == "local":
        return {
            "id": "local",
            "name": "Local host",
            "url": "",
            "description": "The Docker host running this service",
            "type": "local",
        }
    for instance in load_remote_instances():
        if instance["id"] == instance_id:
            return instance
    return None


def proxy_remote_request(instance_id: str, proxy_path: str) -> Response:
    instance = get_instance(instance_id)
    if not instance or instance.get("type") != "remote":
        return jsonify({"status": "error", "message": "Instance not found or not remote"}), 404

    allowed_prefixes = (
        "status",
        "images",
        "containers",
        "host",
        "stacks",
        "jobs",
        "operations",
        "check",
        "update",
        "bulk",
        "prune",
        "compose",
        "notify",
        "config",
    )
    if not any(proxy_path.startswith(prefix) for prefix in allowed_prefixes):
        return jsonify({"status": "error", "message": "Unsupported proxy path"}), 400

    remote_url = f"{instance['url']}/api/{proxy_path}"
    try:
        payload = request.get_json(silent=True)
        params = request.args.to_dict(flat=True)
        response = requests.request(
            request.method,
            remote_url,
            json=payload if payload is not None else None,
            params=params,
            timeout=DEFAULT_PROXY_TIMEOUT,
        )
        return Response(response.content, status=response.status_code,
                        content_type=response.headers.get("Content-Type", "application/json"))
    except requests.exceptions.RequestException as e:
        log.warning(f"Remote proxy failed for {instance_id}:{proxy_path}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 502


def proxy_local_request(proxy_path: str) -> Response:
    # Map proxy paths to the actual functions in app module
    if proxy_path == "status":
        with state_lock:
            return jsonify({
                "last_check": get_last_full_check(),
                "total": len(check_results),
                "up_to_date": sum(1 for r in check_results.values()
                                  if r["status"] == STATUS_UP_TO_DATE),
                "updates_available": sum(1 for r in check_results.values()
                                         if r["status"] == STATUS_UPDATE_AVAILABLE),
                "unknown": sum(1 for r in check_results.values()
                               if r["status"] in (STATUS_UNKNOWN, STATUS_REGISTRY_ERROR, STATUS_NOT_PULLED)),
                "check_interval_minutes": CHECK_INTERVAL_MINUTES,
                "auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL,
                "notify_enabled": NOTIFY_ENABLED,
                "notify_backend": NOTIFY_BACKEND or None,
                "version": VERSION
            })
    if proxy_path == "images":
        with state_lock:
            return jsonify(list(check_results.values()))
    if proxy_path == "stacks":
        return jsonify(summarize_stacks())
    if proxy_path == "stacks/all":
        return jsonify(get_all_stacks())
    if proxy_path == "jobs":
        with state_lock:
            jobs = sorted(
                jobs_state.values(),
                key=lambda j: j.get("started_at", ""),
                reverse=True
            )
            return jsonify(jobs[:30])
    if proxy_path == "operations":
        return jsonify(operations_log.latest(50))
    if proxy_path == "config":
        return jsonify({"auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL})
    if proxy_path == "check":
        job_id = create_job("full_check", "all", total_steps=4)
        threading.Thread(target=run_full_check, args=(job_id,), daemon=True).start()
        return jsonify({"status": "started", "job_id": job_id})
    if proxy_path.startswith("check/"):
        image_ref = proxy_path[len("check/"):]
        job_id = create_job("check_image", image_ref, total_steps=2, meta={"image": image_ref})
        update_job(job_id, progress=0, current_step="Checking image", message=f"Checking {image_ref}")
        try:
            result = check_image(image_ref)
            with state_lock:
                if image_ref in check_results:
                    result["compose_files"] = check_results[image_ref].get("compose_files", [])
                else:
                    result["compose_files"] = []
                result["stacks"] = sorted(list({derive_stack_name(p) for p in result["compose_files"]}))
                check_results[image_ref] = result

            log_op("check", image_ref, "success", f"Status: {result['status']}")
            update_job(job_id,
                       progress=1,
                       current_step="Check complete",
                       message=f"Status: {result['status']}",
                       event={"status": "success", "message": f"{image_ref}: {result['status']}"})
            finish_job(job_id, "success", f"{image_ref}: {result['status']}")
            return jsonify({"job_id": job_id, **result})
        except Exception as e:
            log_op("check", image_ref, "error", str(e))
            finish_job(job_id, "error", str(e))
            return jsonify({"status": "error", "message": str(e), "job_id": job_id}), 500
    if proxy_path.startswith("update/"):
        return api_update_image(proxy_path[len("update/"):])
    if proxy_path == "bulk/update":
        data = request.get_json(silent=True) or {}
        stack_name = data.get("stack")
        auto_recreate = data.get("auto_recreate", AUTO_RECREATE_AFTER_PULL)
        target = stack_name or "all"
        job_id = create_job("bulk_pull", target, stack=stack_name, total_steps=1,
                          meta={"stack": stack_name, "auto_recreate": auto_recreate})
        threading.Thread(target=run_bulk_pull, args=(job_id, stack_name, auto_recreate), daemon=True).start()
        return jsonify({"status": "started", "job_id": job_id, "stack": stack_name, "auto_recreate": auto_recreate})
    if proxy_path.startswith("stacks/") and proxy_path.endswith("/recreate"):
        stack_name = proxy_path[len("stacks/"):-len("/recreate")]
        job_id = create_job("recreate_stack", stack_name, stack=stack_name, total_steps=1, meta={"stack": stack_name})
        threading.Thread(target=run_stack_recreate, args=(job_id, stack_name), daemon=True).start()
        return jsonify({"status": "started", "job_id": job_id, "stack": stack_name})
    if proxy_path == "compose/recreate":
        data = request.get_json(silent=True) or {}
        compose_path = data.get("compose_path")
        if not compose_path:
            return jsonify({"status": "error", "message": "compose_path required"}), 400
        compose_file = Path(compose_path)
        if not compose_file.exists():
            return jsonify({"status": "error", "message": "File not found"}), 404
        stack = derive_stack_name(str(compose_file))
        job_id = create_job("recreate_stack", compose_path, stack=stack, total_steps=3, meta={"compose_path": compose_path})
        log_op("recreate", compose_path, "started", "Running docker compose up -d")
        update_job(job_id, progress=0, current_step="Preparing recreate", message=f"Preparing recreate for {compose_path}")
        try:
            update_job(job_id, progress=1, current_step="Running docker compose",
                       message=f"docker compose up -d for {compose_path}",
                       event={"status": "started", "message": f"Recreate started for stack {stack}"})
            r = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"],
                capture_output=True, text=True, timeout=DEFAULT_COMPOSE_TIMEOUT,
                cwd=str(compose_file.parent)
            )
            if r.returncode == 0:
                update_job(job_id, progress=2, current_step="Refreshing stack state",
                           message=f"Refreshing image state for {stack}")
                refreshed = 0
                related_images = []
                with state_lock:
                    for image_ref, item in check_results.items():
                        if compose_path in (item.get("compose_files") or []):
                            related_images.append(image_ref)
                for image_ref in related_images:
                    result = check_image(image_ref)
                    with state_lock:
                        existing = check_results.get(image_ref, {})
                        result["compose_files"] = existing.get("compose_files", [])
                        result["stacks"] = sorted(list({derive_stack_name(p) for p in result["compose_files"]}))
                        check_results[image_ref] = result
                    refreshed += 1
                log_op("recreate", compose_path, "success", r.stdout or "Done")
                notify_recreate_result(compose_path, ok=True, message=r.stdout or "Recreate completed", stack=stack)
                finish_job(job_id, "success", f"Recreated stack {stack}, refreshed {refreshed} images")
                return jsonify({"status": "success", "output": r.stdout, "job_id": job_id})
            else:
                log_op("recreate", compose_path, "error", r.stderr)
                notify_recreate_result(compose_path, ok=False, message=r.stderr, stack=stack)
                finish_job(job_id, "error", r.stderr)
                return jsonify({"status": "error", "message": r.stderr, "job_id": job_id}), 500
        except subprocess.TimeoutExpired:
            log_op("recreate", compose_path, "error", "Timed out")
            notify_recreate_result(compose_path, ok=False, message="Timed out after 300s", stack=stack)
            finish_job(job_id, "error", "Timed out after 300s")
            return jsonify({"status": "error", "message": "Timed out after 300s", "job_id": job_id}), 500
        except Exception as e:
            log_op("recreate", compose_path, "error", str(e))
            notify_recreate_result(compose_path, ok=False, message=str(e), stack=stack)
            finish_job(job_id, "error", str(e))
            return jsonify({"status": "error", "message": str(e), "job_id": job_id}), 500
    if proxy_path.startswith("prune/"):
        prune_type = proxy_path.split("/", 1)[1]
        include_all = False
        if prune_type == "containers":
            job_id = create_job("prune_containers", "containers", total_steps=2, meta={"prune_type": "containers"})
        elif prune_type == "images":
            data = request.get_json(silent=True) or {}
            include_all = bool(data.get("all", False))
            job_id = create_job("prune_images", "images", total_steps=2, meta={"prune_type": "images", "all": include_all})
        elif prune_type == "system":
            job_id = create_job("prune_system", "system", total_steps=2, meta={"prune_type": "system"})
        elif prune_type == "volumes":
            data = request.get_json(silent=True) or {}
            include_all = bool(data.get("all", False))
            job_id = create_job("prune_volumes", "volumes", total_steps=2, meta={"prune_type": "volumes", "all": include_all})
        else:
            return jsonify({"status": "error", "message": "Unsupported prune type"}), 400
        threading.Thread(target=run_prune_job, args=(job_id, prune_type, include_all), daemon=True).start()
        return jsonify({"status": "started", "job_id": job_id, "prune_type": prune_type, "all": include_all})
    if proxy_path == "notify/test":
        try:
            send_notification(
                event_type="test",
                title="Docker Update Checker test notification",
                message="This is a test notification from docker-update-checker.",
                status="info",
                extra={"manual_test": True}
            )
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    if proxy_path == "host/resources":
        resources = get_host_resources()
        return jsonify(resources)
    if proxy_path.startswith("containers") and not proxy_path.startswith("containers/"):
        # Handle /containers?params - call the function directly
        all_containers = request.args.get("all", "false").lower() == "true"
        status_filter = request.args.get("status", None)
        with_resources = request.args.get("resources", "false").lower() == "true"
        
        filters = {}
        if status_filter:
            filters["status"] = status_filter
        
        containers = list_containers(all_containers=all_containers, filters=filters if filters else None)
        
        # If resource data requested, fetch for all containers
        if with_resources and containers:
            resource_data = get_all_container_resources(containers)
            for container in containers:
                container["resources"] = resource_data.get(container["id"], {})
        
        return jsonify(containers)
    if proxy_path.startswith("containers/"):
        # Extract container ID and action from path like "containers/abc123/resources"
        container_id = proxy_path.split("/", 1)[1]
        if proxy_path.endswith("/resources"):
            resources = get_container_resources(container_id)
            if resources is None:
                return jsonify({"status": "error", "message": "Container not found or unavailable"}), 404
            return jsonify(resources)
        elif proxy_path.endswith("/start"):
            return start_container(container_id)
        elif proxy_path.endswith("/stop"):
            return stop_container(container_id)
        elif proxy_path.endswith("/restart"):
            return restart_container(container_id)
        else:
            container_data = inspect_container(container_id)
            if container_data is None:
                return jsonify({"status": "error", "message": "Container not found"}), 404
            return jsonify(container_data)
    if proxy_path == "compose/files/detailed":
        project_filter = request.args.get("project", None)
        files = list_compose_files_detailed()
        if project_filter:
            files = [f for f in files if f.get("project") == project_filter]
        return jsonify(files)
    if proxy_path == "compose/files":
        files = find_compose_files()
        return jsonify(files)
    if proxy_path.startswith("compose/files/") and not any(proxy_path.endswith(suffix) for suffix in ["/validate", "/dependencies"]):
        compose_path = proxy_path[len("compose/files/"):].lstrip('/')
        if request.method == "GET":
            content = get_compose_file_content(compose_path)
            if content is None:
                return jsonify({"status": "error", "message": "File not found"}), 404
            return jsonify({"path": compose_path, "content": content})
        elif request.method == "PUT":
            data = request.get_json(silent=True) or {}
            content = data.get("content", {})
            backup = data.get("backup", True)
            try:
                success, message = write_compose_file(compose_path, content, backup)
                return jsonify({"status": "success" if success else "error", "message": message})
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500
    if proxy_path.endswith("/validate"):
        compose_path = proxy_path.replace("/validate", "").lstrip('/')
        data = request.get_json(silent=True) or {}
        content = data.get("content")
        try:
            if content:
                valid, message, errors = validate_compose_content(content)
                return jsonify({"valid": valid, "message": message, "errors": errors})
            else:
                yaml_content = get_compose_file_content(compose_path)
                if yaml_content:
                    valid, message, errors = validate_compose_content(yaml_content)
                    return jsonify({"valid": valid, "message": message, "errors": errors})
                else:
                    return jsonify({"valid": False, "message": "File not found"}), 404
        except Exception as e:
            return jsonify({"valid": False, "message": str(e)})
    if proxy_path.endswith("/dependencies"):
        compose_path = proxy_path.replace("/dependencies", "").lstrip('/')
        try:
            deps = get_compose_file_dependencies(compose_path)
            return jsonify(deps)
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "error", "message": "Unsupported local proxy path"}), 400


def summarize_stacks() -> list[dict]:
    stacks: dict[str, dict] = {}
    with state_lock:
        results = list(check_results.values())

    for item in results:
        compose_files = item.get("compose_files") or []
        if not compose_files:
            stacks.setdefault("unassigned", {
                "stack": "unassigned",
                "compose_files": [],
                "images": [],
                "total_images": 0,
                "updates_available": 0,
                "up_to_date": 0,
                "unknown": 0,
                "last_checked": None
            })
            targets = ["unassigned"]
        else:
            targets = [derive_stack_name(cf) for cf in compose_files]

        for stack_name in set(targets):
            stack = stacks.setdefault(stack_name, {
                "stack": stack_name,
                "compose_files": [],
                "images": [],
                "total_images": 0,
                "updates_available": 0,
                "up_to_date": 0,
                "unknown": 0,
                "last_checked": None
            })

            stack["images"].append({
                "image": item["image"],
                "status": item["status"],
                "checked_at": item.get("checked_at"),
                "compose_files": compose_files,
            })
            stack["total_images"] += 1

            if item["status"] == STATUS_UPDATE_AVAILABLE:
                stack["updates_available"] += 1
            elif item["status"] == STATUS_UP_TO_DATE:
                stack["up_to_date"] += 1
            else:
                stack["unknown"] += 1

            for cf in compose_files:
                if cf not in stack["compose_files"] and derive_stack_name(cf) == stack_name:
                    stack["compose_files"].append(cf)

            checked = item.get("checked_at")
            if checked and (not stack["last_checked"] or checked > stack["last_checked"]):
                stack["last_checked"] = checked

    return sorted(
        stacks.values(),
        key=lambda s: (-s["updates_available"], s["stack"])
    )

def refresh_image_result(image_ref: str):
    result = check_image(image_ref)
    with state_lock:
        existing = check_results.get(image_ref, {})
        result["compose_files"] = existing.get("compose_files", [])
        result["stacks"] = sorted(list({derive_stack_name(p) for p in result["compose_files"]}))
        check_results[image_ref] = result
    return result


def get_images_for_stack(stack_name: str) -> list[str]:
    with state_lock:
        items = list(check_results.values())

    images = []
    for item in items:
        stacks = item.get("stacks") or []
        if stack_name in stacks:
            images.append(item["image"])
    return sorted(list(set(images)))


def get_outdated_images(stack_name: Optional[str] = None) -> list[str]:
    with state_lock:
        items = list(check_results.values())

    images = []
    for item in items:
        if item.get("status") not in (STATUS_UPDATE_AVAILABLE, STATUS_NOT_PULLED):
            continue
        if stack_name and stack_name not in (item.get("stacks") or []):
            continue
        images.append(item["image"])
    return sorted(list(set(images)))


def run_full_check(job_id: Optional[str] = None):
    log.info("Running full image check...")

    if not job_id:
        job_id = create_job("full_check", "all", total_steps=4)

    update_job(job_id, progress=0, current_step="Scanning compose files",
               message="Looking for compose files")
    compose_files = find_compose_files()
    update_job(job_id, event={
        "status": "info",
        "message": f"Found {len(compose_files)} compose files"
    })

    all_images: dict[str, list[str]] = {}
    total_compose = max(len(compose_files), 1)

    for idx, cf in enumerate(compose_files, start=1):
        update_job(
            job_id,
            progress=1,
            current_step=f"Parsing compose files ({idx}/{total_compose})",
            message=f"Parsing {cf['path']}",
            event={"status": "info", "message": f"Parsing compose file {cf['path']}"}
        )
        for img in parse_images_from_compose(cf["path"]):
            all_images.setdefault(img, []).append(cf["path"])

    results = {}
    images = list(all_images.items())
    total_images = len(images)
    progress_base = 2

    update_job(job_id, progress=progress_base,
               current_step="Checking image digests",
               message=f"Checking {total_images} images")

    if total_images > 0:
        with ThreadPoolExecutor(max_workers=min(10, total_images)) as executor:
            future_map = {
                executor.submit(check_image, img): (img, paths)
                for img, paths in images
            }
            completed = 0
            for future in as_completed(future_map):
                img, paths = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    log.warning(f"Image check failed for {img}: {exc}")
                    result = {
                        "image": img,
                        "status": "unknown",
                        "local_digest": None,
                        "remote_digest": None,
                        "checked_at": datetime.now(timezone.utc).isoformat()
                    }
                result["compose_files"] = paths
                result["stacks"] = sorted(list({derive_stack_name(p) for p in paths}))
                results[img] = result
                completed += 1

                if completed == 1 or completed == total_images or completed % 5 == 0:
                    update_job(
                        job_id,
                        progress=progress_base,
                        current_step=f"Checking image digests ({completed}/{total_images})",
                        message=f"Checked {completed} of {total_images} images",
                        event={"status": "info", "message": f"Checked {img}: {result['status']}"}
                    )
    else:
        update_job(job_id, progress=progress_base,
                   current_step="Checking image digests",
                   message="No images found")

    update_job(job_id, progress=3, current_step="Saving results",
               message="Updating in-memory state")

    with state_lock:
        check_results.clear()
        check_results.update(results)
        set_last_full_check(datetime.now(timezone.utc).isoformat())

    updates = sum(1 for r in results.values() if r["status"] == "update_available")
    log.info(f"Check complete: {len(results)} images, {updates} updates available.")
    log_op("check", "all", "success",
           f"Checked {len(results)} images, {updates} updates available")

    notify_updates_found(results)

    finish_job(
        job_id,
        status="success",
        message=f"Checked {len(results)} images, {updates} updates available"
    )

def run_bulk_pull(job_id: str, stack_name: Optional[str] = None, auto_recreate: bool = False):
    label = stack_name or "all"
    images = get_outdated_images(stack_name=stack_name)

    client = docker_client()
    if not client:
        finish_job(job_id, "error", "Docker socket not connected")
        log_op("bulk_pull", label, "error", "Docker socket not connected")
        notify_bulk_complete(label, "Bulk pull failed: Docker socket not connected", {
            "stack": stack_name,
            "success_count": 0,
            "total_images": 0,
            "auto_recreate": auto_recreate
        })
        return

    if not images:
        finish_job(job_id, "success", f"No outdated images found for {label}")
        log_op("bulk_pull", label, "success", f"No outdated images found for {label}")
        return

    steps = len(images) + (1 if auto_recreate else 0)
    with state_lock:
        if job_id in jobs_state:
            jobs_state[job_id]["total_steps"] = steps

    updated_images = []
    affected_compose_files = set()

    for idx, image_ref in enumerate(images, start=1):
        update_job(
            job_id,
            progress=idx - 1,
            current_step=f"Pulling image {idx}/{len(images)}",
            message=image_ref,
            event={"status": "started", "message": f"Pulling {image_ref}"}
        )
        log_op("bulk_pull", image_ref, "started", f"Pulling {image_ref}")

        try:
            client.images.pull(image_ref)
            result = refresh_image_result(image_ref)
            updated_images.append(image_ref)
            for cf in result.get("compose_files", []):
                affected_compose_files.add(cf)

            log_op("bulk_pull", image_ref, "success", f"Pulled {image_ref}")
            notify_pull_result(
                image_ref,
                ok=True,
                message="Image pulled successfully during bulk job",
                stacks=result.get("stacks", [])
            )
            update_job(
                job_id,
                progress=idx,
                current_step=f"Pulled image {idx}/{len(images)}",
                message=image_ref,
                event={"status": "success", "message": f"Pulled {image_ref} ({result['status']})"}
            )
        except Exception as e:
            log_op("bulk_pull", image_ref, "error", str(e))
            notify_pull_result(image_ref, ok=False, message=str(e))
            update_job(
                job_id,
                progress=idx,
                current_step=f"Pull failed for {image_ref}",
                message=str(e),
                event={"status": "error", "message": f"{image_ref}: {e}"}
            )

    if auto_recreate and affected_compose_files:
        update_job(
            job_id,
            progress=len(images),
            current_step="Auto-recreating affected services",
            message=f"{len(affected_compose_files)} compose file(s)",
            event={"status": "started", "message": "Starting auto-recreate phase"}
        )

        recreate_results = []
        for compose_path in sorted(affected_compose_files):
            target_services = []
            for image_ref in updated_images:
                target_services.extend(get_services_for_image(compose_path, image_ref))
            target_services = sorted(list(set(target_services)))
            stack = derive_stack_name(compose_path)

            try:
                result = recreate_compose(compose_path, services=target_services or None)
                if result.returncode == 0:
                    recreate_results.append((compose_path, "success"))
                    log_op("auto_recreate", compose_path, "success", result.stdout or "Done")
                    notify_recreate_result(
                        compose_path,
                        ok=True,
                        message=result.stdout or "Recreate completed",
                        stack=stack
                    )
                    update_job(
                        job_id,
                        event={"status": "success",
                               "message": f"Recreated {compose_path} ({', '.join(target_services) if target_services else 'full stack'})"}
                    )
                else:
                    recreate_results.append((compose_path, "error"))
                    log_op("auto_recreate", compose_path, "error", result.stderr)
                    notify_recreate_result(
                        compose_path,
                        ok=False,
                        message=result.stderr,
                        stack=stack
                    )
                    update_job(
                        job_id,
                        event={"status": "error",
                               "message": f"Recreate failed for {compose_path}: {result.stderr}"}
                    )
            except Exception as e:
                recreate_results.append((compose_path, "error"))
                log_op("auto_recreate", compose_path, "error", str(e))
                notify_recreate_result(
                    compose_path,
                    ok=False,
                    message=str(e),
                    stack=stack
                )
                update_job(
                    job_id,
                    event={"status": "error",
                           "message": f"Recreate exception for {compose_path}: {e}"}
                )

        for image_ref in updated_images:
            try:
                refresh_image_result(image_ref)
            except Exception:
                pass

        update_job(
            job_id,
            progress=steps,
            current_step="Auto-recreate complete",
            message=f"Processed {len(recreate_results)} compose file(s)"
        )

    success_count = len(updated_images)
    summary = (
        f"Bulk pull complete for {label}: {success_count}/{len(images)} images pulled"
        + (" with auto-recreate" if auto_recreate else "")
    )

    finish_job(job_id, "success", summary)
    log_op("bulk_pull", label, "success", summary)
    notify_bulk_complete(label, summary, {
        "stack": stack_name,
        "success_count": success_count,
        "total_images": len(images),
        "updated_images": updated_images,
        "auto_recreate": auto_recreate
    })

def run_stack_recreate(job_id: str, stack_name: str):
    stacks = summarize_stacks()
    stack = next((s for s in stacks if s["stack"] == stack_name), None)

    if not stack:
        finish_job(job_id, "error", f"Stack not found: {stack_name}")
        log_op("recreate_stack", stack_name, "error", "Stack not found")
        notify_recreate_result(stack_name, ok=False, message="Stack not found", stack=stack_name)
        return

    compose_files = stack.get("compose_files", [])
    if not compose_files:
        finish_job(job_id, "error", f"No compose files found for stack {stack_name}")
        log_op("recreate_stack", stack_name, "error", "No compose files found")
        notify_recreate_result(stack_name, ok=False, message="No compose files found", stack=stack_name)
        return

    with state_lock:
        if job_id in jobs_state:
            jobs_state[job_id]["total_steps"] = len(compose_files)

    for idx, compose_path in enumerate(compose_files, start=1):
        update_job(
            job_id,
            progress=idx - 1,
            current_step=f"Recreating compose file {idx}/{len(compose_files)}",
            message=compose_path,
            event={"status": "started", "message": f"Recreating {compose_path}"}
        )
        try:
            r = recreate_compose(compose_path)
            if r.returncode == 0:
                log_op("recreate_stack", compose_path, "success", r.stdout or "Done")
                notify_recreate_result(compose_path, ok=True, message=r.stdout or "Recreate completed", stack=stack_name)
                update_job(
                    job_id,
                    progress=idx,
                    current_step=f"Recreated compose file {idx}/{len(compose_files)}",
                    message=compose_path,
                    event={"status": "success", "message": f"Recreated {compose_path}"}
                )
            else:
                log_op("recreate_stack", compose_path, "error", r.stderr)
                notify_recreate_result(compose_path, ok=False, message=r.stderr, stack=stack_name)
                update_job(
                    job_id,
                    progress=idx,
                    current_step=f"Recreate failed for {compose_path}",
                    message=r.stderr,
                    event={"status": "error", "message": f"{compose_path}: {r.stderr}"}
                )
        except Exception as e:
            log_op("recreate_stack", compose_path, "error", str(e))
            notify_recreate_result(compose_path, ok=False, message=str(e), stack=stack_name)
            update_job(
                job_id,
                progress=idx,
                current_step=f"Recreate failed for {compose_path}",
                message=str(e),
                event={"status": "error", "message": f"{compose_path}: {e}"}
            )

    for image_ref in get_images_for_stack(stack_name):
        try:
            refresh_image_result(image_ref)
        except Exception:
            pass

    finish_job(job_id, "success", f"Stack recreate complete for {stack_name}")
    log_op("recreate_stack", stack_name, "success", f"Stack recreate complete for {stack_name}")

def run_prune_command(args: list[str], timeout: int = DEFAULT_PRUNE_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout
    )


def run_prune_job(job_id: str, prune_type: str, include_all: bool = False):
    client = docker_client()
    if not client:
        finish_job(job_id, "error", "Docker socket not connected")
        log_op("prune", prune_type, "error", "Docker socket not connected")
        return

    cmd = ["docker"]
    description = ""
    meta = {"prune_type": prune_type, "include_all": include_all}

    if prune_type == "containers":
        cmd += ["container", "prune", "-f"]
        description = "Remove stopped containers"
    elif prune_type == "images":
        cmd += ["image", "prune", "-f"]
        if include_all:
            cmd.append("-a")
            description = "Remove all unused images"
        else:
            description = "Remove dangling images"
    elif prune_type == "system":
        cmd += ["system", "prune", "-f"]
        description = "Remove stopped containers, unused networks, dangling images, and build cache"
    elif prune_type == "volumes":
        cmd += ["volume", "prune", "-f"]
        if include_all:
            cmd.append("-a")
            description = "Remove all unused local volumes"
        else:
            description = "Remove anonymous unused local volumes"
    else:
        finish_job(job_id, "error", f"Unsupported prune type: {prune_type}")
        log_op("prune", prune_type, "error", f"Unsupported prune type: {prune_type}")
        return

    update_job(
        job_id,
        progress=0,
        current_step="Preparing prune",
        message=description,
        event={"status": "info", "message": f"Preparing {prune_type} prune"}
    )
    log_op("prune", prune_type, "started", description)

    try:
        update_job(
            job_id,
            progress=1,
            current_step="Running prune command",
            message=" ".join(cmd),
            event={"status": "started", "message": f"Running {' '.join(cmd)}"}
        )

        result = run_prune_command(cmd, timeout=DEFAULT_PRUNE_TIMEOUT)
        output = (result.stdout or "").strip()
        error_output = (result.stderr or "").strip()

        if result.returncode == 0:
            final_message = output or f"{description} completed"
            update_job(
                job_id,
                progress=2,
                current_step="Prune complete",
                message=final_message,
                event={"status": "success", "message": final_message}
            )
            log_op("prune", prune_type, "success", final_message)
            finish_job(job_id, "success", final_message)
        else:
            final_message = error_output or output or f"{description} failed"
            update_job(
                job_id,
                progress=2,
                current_step="Prune failed",
                message=final_message,
                event={"status": "error", "message": final_message}
            )
            log_op("prune", prune_type, "error", final_message)
            finish_job(job_id, "error", final_message)

    except subprocess.TimeoutExpired:
        message = "Timed out after 600s"
        log_op("prune", prune_type, "error", message)
        update_job(
            job_id,
            progress=2,
            current_step="Prune timed out",
            message=message,
            event={"status": "error", "message": message}
        )
        finish_job(job_id, "error", message)
    except Exception as e:
        message = str(e)
        log_op("prune", prune_type, "error", message)
        update_job(
            job_id,
            progress=2,
            current_step="Prune failed",
            message=message,
            event={"status": "error", "message": message}
        )
        finish_job(job_id, "error", message)

# Import API routes module to register all Flask routes
import api

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(run_full_check, "interval",
                  minutes=CHECK_INTERVAL_MINUTES, id="full_check")
# Clean up expired registry tokens every hour to prevent memory leaks
from config import cleanup_token_cache
scheduler.add_job(cleanup_token_cache, "interval",
                  hours=1, id="token_cache_cleanup")
scheduler.start()

threading.Thread(
    target=run_full_check,
    args=(create_job("startup_check", "all", total_steps=4),),
    daemon=True
).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
