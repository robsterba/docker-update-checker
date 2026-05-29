import os
import sys
import json
import logging
import socket
import threading
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import re
import uuid
import requests
import docker
import yaml
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.message import EmailMessage

# ── Import configuration from config.py (single source of truth) ──────────────
from config import (
    COMPOSE_ROOT,
    CHECK_INTERVAL_MINUTES,
    LOG_LEVEL,
    AUTO_RECREATE_AFTER_PULL,
    NOTIFY_ENABLED,
    NOTIFY_BACKEND,
    NOTIFY_WEBHOOK_URL,
    NOTIFY_WEBHOOK_METHOD,
    NOTIFY_WEBHOOK_TIMEOUT,
    NOTIFY_MQTT_HOST,
    NOTIFY_MQTT_PORT,
    NOTIFY_MQTT_TOPIC,
    NOTIFY_MQTT_USERNAME,
    NOTIFY_MQTT_PASSWORD,
    NOTIFY_MQTT_RETAIN,
    NOTIFY_EMAIL_HOST,
    NOTIFY_EMAIL_PORT,
    NOTIFY_EMAIL_USERNAME,
    NOTIFY_EMAIL_PASSWORD,
    NOTIFY_EMAIL_FROM,
    NOTIFY_EMAIL_TO,
    NOTIFY_EMAIL_USE_TLS,
    NOTIFY_ON_UPDATES_FOUND,
    NOTIFY_ON_PULL_SUCCESS,
    NOTIFY_ON_PULL_ERROR,
    NOTIFY_ON_RECREATE_SUCCESS,
    NOTIFY_ON_RECREATE_ERROR,
    NOTIFY_ON_BULK_COMPLETE,
    REMOTE_INSTANCES_CONFIG,
    REMOTE_INSTANCES_FILE,
    TOKEN_CACHE_TTL,
    REGISTRY_TOKEN_CACHE,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
# When running app.py as a script, alias __main__ to app so route imports work consistently.
sys.modules["app"] = sys.modules[__name__]
CORS(app)

docker_client: Optional[docker.DockerClient] = None
try:
    docker_client = docker.from_env()
    docker_client.ping()
    log.info("Docker socket connected.")
except Exception as e:
    log.warning(f"Docker socket unavailable: {e}")

# ── In-memory state ───────────────────────────────────────────────────────────
state_lock = threading.Lock()
check_results: dict[str, dict[str, Any]] = {}
last_full_check: Optional[str] = None


class OperationLog:
    def __init__(self, max_entries: int = 200):
        self._entries: list[dict[str, Any]] = []
        self.max_entries = max_entries

    def log(self, action: str, target: str, status: str, message: str) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "target": target,
            "status": status,
            "message": message,
        }
        with state_lock:
            self._entries.insert(0, entry)
            if len(self._entries) > self.max_entries:
                self._entries.pop()

    def latest(self, limit: int = 50) -> list[dict[str, Any]]:
        with state_lock:
            return self._entries[:limit]


class JobManager:
    def __init__(self, max_entries: int = 100):
        self.jobs_state: dict[str, dict[str, Any]] = {}
        self.max_entries = max_entries

    def create_job(self, job_type: str, target: str, stack: Optional[str] = None,
                   total_steps: int = 1, meta: Optional[dict[str, Any]] = None) -> str:
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "type": job_type,
            "target": target,
            "stack": stack,
            "status": "running",
            "progress": 0,
            "total_steps": max(total_steps, 1),
            "current_step": "Starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "message": "",
            "meta": meta or {},
            "events": [],
        }
        with state_lock:
            self.jobs_state[job_id] = job
            self._trim_jobs_locked()
        return job_id

    def update_job(self, job_id: str, progress: Optional[int] = None,
                   current_step: Optional[str] = None,
                   message: Optional[str] = None,
                   event: Optional[dict[str, Any]] = None,
                   status: Optional[str] = None) -> None:
        with state_lock:
            job = self.jobs_state.get(job_id)
            if not job:
                return
            if progress is not None:
                job["progress"] = max(0, min(progress, job["total_steps"]))
            if current_step is not None:
                job["current_step"] = current_step
            if message is not None:
                job["message"] = message
            if status is not None:
                job["status"] = status
            if event:
                entry = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    **event,
                }
                job["events"].insert(0, entry)
                if len(job["events"]) > 100:
                    job["events"].pop()

    def finish_job(self, job_id: str, status: str = "success", message: str = "") -> None:
        with state_lock:
            job = self.jobs_state.get(job_id)
            if not job:
                return
            job["status"] = status
            job["progress"] = job["total_steps"]
            job["message"] = message or job.get("message", "")
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            job["events"].insert(0, {
                "time": job["finished_at"],
                "status": status,
                "message": job["message"] or f"Job finished with status: {status}",
            })
            if len(job["events"]) > 100:
                job["events"].pop()
            self._trim_jobs_locked()

    def _trim_jobs_locked(self) -> None:
        if len(self.jobs_state) <= self.max_entries:
            return
        ordered = sorted(
            self.jobs_state.items(),
            key=lambda kv: kv[1].get("started_at", ""),
            reverse=True,
        )
        keep_ids = {job_id for job_id, _ in ordered[: self.max_entries]}
        for job_id in list(self.jobs_state.keys()):
            if job_id not in keep_ids:
                self.jobs_state.pop(job_id, None)


operations_log = OperationLog()
job_manager = JobManager()
jobs_state = job_manager.jobs_state

# ── Helpers ───────────────────────────────────────────────────────────────────

def log_op(action: str, target: str, status: str, message: str) -> None:
    operations_log.log(action, target, status, message)

def derive_stack_name(compose_path: str) -> str:
    p = Path(compose_path)
    return p.parent.name or "default"


def create_job(job_type: str, target: str, stack: Optional[str] = None,
               total_steps: int = 1, meta: Optional[dict[str, Any]] = None) -> str:
    return job_manager.create_job(job_type, target, stack, total_steps, meta)


def update_job(job_id: str, progress: Optional[int] = None,
               current_step: Optional[str] = None,
               message: Optional[str] = None,
               event: Optional[dict[str, Any]] = None,
               status: Optional[str] = None) -> None:
    job_manager.update_job(job_id, progress, current_step, message, event, status)


def finish_job(job_id: str, status: str = "success", message: str = "") -> None:
    job_manager.finish_job(job_id, status, message)


def get_registry_token(registry: str, repo: str) -> Optional[str]:
    cache_key = f"{registry}:{repo}"
    cached = REGISTRY_TOKEN_CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > time.time():
        return cached["token"]

    token = None
    try:
        if registry in ("registry-1.docker.io", "docker.io"):
            if '/' not in repo:
                repo = f"library/{repo}"
            r = requests.get(
                "https://auth.docker.io/token",
                params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
                timeout=15,
            )
            r.raise_for_status()
            token = r.json().get("token")
        elif registry == "ghcr.io":
            r = requests.get(
                "https://ghcr.io/token",
                params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"},
                timeout=15,
            )
            r.raise_for_status()
            token = r.json().get("token")
    except Exception as e:
        log.debug(f"Token retrieval failed for {registry}/{repo}: {e}")
        return None

    if token:
        REGISTRY_TOKEN_CACHE[cache_key] = {
            "token": token,
            "expires_at": time.time() + TOKEN_CACHE_TTL,
        }
    return token


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
            timeout=15,
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
                "last_check": last_full_check,
                "total": len(check_results),
                "up_to_date": sum(1 for r in check_results.values()
                                  if r["status"] == "up_to_date"),
                "updates_available": sum(1 for r in check_results.values()
                                         if r["status"] == "update_available"),
                "unknown": sum(1 for r in check_results.values()
                               if r["status"] in ("unknown", "registry_error", "not_pulled")),
                "check_interval_minutes": CHECK_INTERVAL_MINUTES,
                "auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL,
                "notify_enabled": NOTIFY_ENABLED,
                "notify_backend": NOTIFY_BACKEND or None
            })
    if proxy_path == "images":
        with state_lock:
            return jsonify(list(check_results.values()))
    if proxy_path == "stacks":
        return jsonify(summarize_stacks())
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
                capture_output=True, text=True, timeout=300,
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

            if item["status"] == "update_available":
                stack["updates_available"] += 1
            elif item["status"] == "up_to_date":
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

def find_compose_files() -> list[dict]:
    root = Path(COMPOSE_ROOT)
    files = []
    for pattern in ("docker-compose.yml", "docker-compose.yaml",
                    "compose.yml", "compose.yaml"):
        for p in root.rglob(pattern):
            files.append({"path": str(p), "project": p.parent.name})
    return files


def resolve_env_vars(value: str, env: dict) -> str:
    """Resolve ${VAR:-default} and ${VAR} patterns."""
    def replacer(m):
        var, _, default = m.group(1).partition(':-')
        return env.get(var, default if default else m.group(0))
    return re.sub(r'\$\{([^}]+)\}', replacer, value)



def parse_images_from_compose(path: str) -> list[str]:
    try:
        env = read_dotenv(Path(path).parent / ".env")

        with open(path) as f:
            data = yaml.safe_load(f)

        images = []
        for svc in (data.get("services") or {}).values():
            img = svc.get("image")
            if not img:
                continue

            img = resolve_env_vars(img, env)

            # Skip still-unresolved shell variables
            if '${' in img:
                log.debug(f"Skipping unresolved image ref: {img}")
                continue

            # Strip digest pin — compare by tag only
            if '@sha256:' in img:
                img = img.split('@')[0]

            # Skip docker-update-checker image (built locally on each system)
            if 'docker-update-checker' in img:
                log.debug(f"Skipping docker-update-checker image: {img}")
                continue

            images.append(img)

        return list(set(images))
    except Exception as e:
        log.warning(f"Failed to parse {path}: {e}")
        return []

def get_services_for_image(compose_path: str, image_ref: str) -> list[str]:
    try:
        env = read_dotenv(Path(compose_path).parent / ".env")

        with open(compose_path) as f:
            data = yaml.safe_load(f) or {}

        matches = []
        for svc_name, svc in (data.get("services") or {}).items():
            img = svc.get("image")
            if not img:
                continue
            img = resolve_env_vars(img, env)
            if '${' in img:
                continue
            if '@sha256:' in img:
                img = img.split('@')[0]
            if img == image_ref:
                matches.append(svc_name)
        return matches
    except Exception as e:
        log.warning(f"Failed to map services for image {image_ref} in {compose_path}: {e}")
        return []


def recreate_compose(compose_path: str, services: Optional[list[str]] = None,
                     remove_orphans: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    compose_file = Path(compose_path)
    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d"]

    if remove_orphans and not services:
        cmd.append("--remove-orphans")

    if services:
        cmd.append("--no-deps")
        cmd.extend(services)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(compose_file.parent)
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
        if item.get("status") not in ("update_available", "not_pulled"):
            continue
        if stack_name and stack_name not in (item.get("stacks") or []):
            continue
        images.append(item["image"])
    return sorted(list(set(images)))


def parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Returns (registry, repo, tag)."""
    tag = "latest"
    ref = image_ref
    if ":" in ref.split("/")[-1]:
        ref, tag = ref.rsplit(":", 1)
    if "/" not in ref:
        return "registry-1.docker.io", f"library/{ref}", tag
    elif "." in ref.split("/")[0] or ":" in ref.split("/")[0]:
        parts = ref.split("/", 1)
        return parts[0], parts[1], tag
    else:
        return "registry-1.docker.io", ref, tag



def get_remote_digest(image_ref: str) -> Optional[str]:
    registry, repo, tag = parse_image_ref(image_ref)
    accept = (
        "application/vnd.docker.distribution.manifest.v2+json,"
        "application/vnd.oci.image.manifest.v1+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json,"
        "application/vnd.oci.image.index.v1+json"
    )
    try:
        headers = {"Accept": accept}
        token = get_registry_token(registry, repo)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if registry in ("registry-1.docker.io", "docker.io"):
            if '/' not in repo:
                repo = f"library/{repo}"
            url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
        elif registry == "ghcr.io":
            url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
        else:
            url = f"https://{registry}/v2/{repo}/manifests/{tag}"

        r2 = requests.head(url, headers=headers, timeout=15)
        r2.raise_for_status()
        return (
            r2.headers.get("Docker-Content-Digest")
            or r2.headers.get("Etag", "").strip('"')
        )
    except Exception as e:
        log.warning(f"Remote digest failed for {image_ref}: {e}")
        return None


def get_local_digest(image_ref: str) -> Optional[str]:
    if not docker_client:
        return None
    try:
        img = docker_client.images.get(image_ref)
        digests = img.attrs.get("RepoDigests", [])
        return digests[0].split("@")[-1] if digests else img.id
    except docker.errors.ImageNotFound:
        return None
    except Exception as e:
        log.warning(f"Local digest error for {image_ref}: {e}")
        return None


def check_image(image_ref: str) -> dict:
    local = get_local_digest(image_ref)
    remote = get_remote_digest(image_ref)
    now = datetime.now(timezone.utc).isoformat()

    if local is None and remote is None:
        status = "unknown"
    elif local is None:
        status = "not_pulled"
    elif remote is None:
        status = "registry_error"
    elif local == remote:
        status = "up_to_date"
    else:
        status = "update_available"

    return {
        "image": image_ref, "status": status,
        "local_digest": local, "remote_digest": remote,
        "checked_at": now
    }


def run_full_check(job_id: Optional[str] = None):
    global last_full_check
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
        last_full_check = datetime.now(timezone.utc).isoformat()

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

    if not docker_client:
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
            docker_client.images.pull(image_ref)
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

def build_notification_payload(event_type: str, title: str, message: str,
                               status: str = "info", extra: Optional[dict] = None) -> dict[str, Any]:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "title": title,
        "message": message,
        "status": status,
        "host": socket.gethostname(),
        "app": "docker-update-checker",
        "extra": extra or {}
    }


def notify_webhook(payload: dict):
    if not NOTIFY_WEBHOOK_URL:
        raise RuntimeError("NOTIFY_WEBHOOK_URL not configured")

    method = NOTIFY_WEBHOOK_METHOD if NOTIFY_WEBHOOK_METHOD in ("POST", "PUT") else "POST"
    headers = {"Content-Type": "application/json"}

    if method == "PUT":
        r = requests.put(NOTIFY_WEBHOOK_URL, json=payload, headers=headers, timeout=NOTIFY_WEBHOOK_TIMEOUT)
    else:
        r = requests.post(NOTIFY_WEBHOOK_URL, json=payload, headers=headers, timeout=NOTIFY_WEBHOOK_TIMEOUT)

    r.raise_for_status()


def notify_mqtt(payload: dict):
    if not NOTIFY_MQTT_HOST or not NOTIFY_MQTT_TOPIC:
        raise RuntimeError("NOTIFY_MQTT_HOST or NOTIFY_MQTT_TOPIC not configured")

    import paho.mqtt.client as mqtt

    client = mqtt.Client()
    if NOTIFY_MQTT_USERNAME:
        client.username_pw_set(NOTIFY_MQTT_USERNAME, NOTIFY_MQTT_PASSWORD or None)

    client.connect(NOTIFY_MQTT_HOST, NOTIFY_MQTT_PORT, 10)
    client.loop_start()
    result = client.publish(
        NOTIFY_MQTT_TOPIC,
        json.dumps(payload),
        qos=0,
        retain=NOTIFY_MQTT_RETAIN
    )
    result.wait_for_publish()
    client.loop_stop()
    client.disconnect()


def notify_email(payload: dict):
    if not all([NOTIFY_EMAIL_HOST, NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO]):
        raise RuntimeError("Email notification settings incomplete")

    msg = EmailMessage()
    msg["Subject"] = f"[{payload.get('status', 'info').upper()}] {payload.get('title', 'Notification')}"
    msg["From"] = NOTIFY_EMAIL_FROM
    msg["To"] = NOTIFY_EMAIL_TO

    body = [
        payload.get("title", ""),
        "",
        payload.get("message", ""),
        "",
        f"Event Type: {payload.get('event_type', '')}",
        f"Status: {payload.get('status', '')}",
        f"Time: {payload.get('time', '')}",
        f"Host: {payload.get('host', '')}",
        "",
        json.dumps(payload.get("extra", {}), indent=2),
    ]
    msg.set_content("\n".join(body))

    with smtplib.SMTP(NOTIFY_EMAIL_HOST, NOTIFY_EMAIL_PORT, timeout=15) as server:
        if NOTIFY_EMAIL_USE_TLS:
            server.starttls()
        if NOTIFY_EMAIL_USERNAME:
            server.login(NOTIFY_EMAIL_USERNAME, NOTIFY_EMAIL_PASSWORD)
        server.send_message(msg)


def send_notification(event_type: str, title: str, message: str,
                      status: str = "info", extra: Optional[dict] = None):
    if not NOTIFY_ENABLED:
        return

    payload = build_notification_payload(event_type, title, message, status, extra)

    try:
        if NOTIFY_BACKEND == "webhook":
            notify_webhook(payload)
        elif NOTIFY_BACKEND == "mqtt":
            notify_mqtt(payload)
        elif NOTIFY_BACKEND == "email":
            notify_email(payload)
        else:
            raise RuntimeError(f"Unsupported NOTIFY_BACKEND: {NOTIFY_BACKEND}")

        log_op("notify", event_type, "success", f"{NOTIFY_BACKEND}: {title}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")
        log_op("notify", event_type, "error", f"{NOTIFY_BACKEND or 'unknown'}: {e}")

def notify_updates_found(results: dict):
    if not NOTIFY_ON_UPDATES_FOUND:
        return

    updates = [r for r in results.values() if r["status"] == "update_available"]
    if not updates:
        return

    send_notification(
        event_type="updates_found",
        title=f"{len(updates)} image update(s) available",
        message="New container image updates were detected.",
        status="info",
        extra={
            "count": len(updates),
            "images": [r["image"] for r in updates],
            "stacks": sorted(list({s for r in updates for s in r.get("stacks", [])}))
        }
    )


def notify_pull_result(image_ref: str, ok: bool, message: str, stacks: Optional[list] = None):
    if ok and not NOTIFY_ON_PULL_SUCCESS:
        return
    if (not ok) and not NOTIFY_ON_PULL_ERROR:
        return

    send_notification(
        event_type="pull_result",
        title=f"Pull {'succeeded' if ok else 'failed'}: {image_ref}",
        message=message,
        status="success" if ok else "error",
        extra={"image": image_ref, "stacks": stacks or []}
    )


def notify_recreate_result(target: str, ok: bool, message: str, stack: Optional[str] = None):
    if ok and not NOTIFY_ON_RECREATE_SUCCESS:
        return
    if (not ok) and not NOTIFY_ON_RECREATE_ERROR:
        return

    send_notification(
        event_type="recreate_result",
        title=f"Recreate {'succeeded' if ok else 'failed'}: {target}",
        message=message,
        status="success" if ok else "error",
        extra={"target": target, "stack": stack}
    )


def notify_bulk_complete(target: str, message: str, extra: Optional[dict] = None):
    if not NOTIFY_ON_BULK_COMPLETE:
        return

    send_notification(
        event_type="bulk_complete",
        title=f"Bulk job complete: {target}",
        message=message,
        status="success",
        extra=extra or {}
    )

def run_prune_command(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout
    )


def run_prune_job(job_id: str, prune_type: str, include_all: bool = False):
    if not docker_client:
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

        result = run_prune_command(cmd, timeout=600)
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
scheduler.start()

threading.Thread(
    target=run_full_check,
    args=(create_job("startup_check", "all", total_steps=4),),
    daemon=True
).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
