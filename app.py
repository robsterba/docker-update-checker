import os
import json
import logging
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import re
import uuid
import requests
import docker
import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.message import EmailMessage

# ── Config ────────────────────────────────────────────────────────────────────
COMPOSE_ROOT = os.environ.get("COMPOSE_ROOT", "/compose")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
AUTO_RECREATE_AFTER_PULL = os.environ.get("AUTO_RECREATE_AFTER_PULL", "false").lower() == "true"
NOTIFY_ENABLED = os.environ.get("NOTIFY_ENABLED", "false").lower() == "true"
NOTIFY_BACKEND = os.environ.get("NOTIFY_BACKEND", "").strip().lower()

NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
NOTIFY_WEBHOOK_METHOD = os.environ.get("NOTIFY_WEBHOOK_METHOD", "POST").strip().upper()
NOTIFY_WEBHOOK_TIMEOUT = int(os.environ.get("NOTIFY_WEBHOOK_TIMEOUT", "10"))

NOTIFY_MQTT_HOST = os.environ.get("NOTIFY_MQTT_HOST", "").strip()
NOTIFY_MQTT_PORT = int(os.environ.get("NOTIFY_MQTT_PORT", "1883"))
NOTIFY_MQTT_TOPIC = os.environ.get("NOTIFY_MQTT_TOPIC", "").strip()
NOTIFY_MQTT_USERNAME = os.environ.get("NOTIFY_MQTT_USERNAME", "").strip()
NOTIFY_MQTT_PASSWORD = os.environ.get("NOTIFY_MQTT_PASSWORD", "").strip()
NOTIFY_MQTT_RETAIN = os.environ.get("NOTIFY_MQTT_RETAIN", "false").lower() == "true"

NOTIFY_EMAIL_HOST = os.environ.get("NOTIFY_EMAIL_HOST", "").strip()
NOTIFY_EMAIL_PORT = int(os.environ.get("NOTIFY_EMAIL_PORT", "587"))
NOTIFY_EMAIL_USERNAME = os.environ.get("NOTIFY_EMAIL_USERNAME", "").strip()
NOTIFY_EMAIL_PASSWORD = os.environ.get("NOTIFY_EMAIL_PASSWORD", "").strip()
NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "").strip()
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
NOTIFY_EMAIL_USE_TLS = os.environ.get("NOTIFY_EMAIL_USE_TLS", "true").lower() == "true"

NOTIFY_ON_UPDATES_FOUND = os.environ.get("NOTIFY_ON_UPDATES_FOUND", "true").lower() == "true"
NOTIFY_ON_PULL_SUCCESS = os.environ.get("NOTIFY_ON_PULL_SUCCESS", "false").lower() == "true"
NOTIFY_ON_PULL_ERROR = os.environ.get("NOTIFY_ON_PULL_ERROR", "true").lower() == "true"
NOTIFY_ON_RECREATE_SUCCESS = os.environ.get("NOTIFY_ON_RECREATE_SUCCESS", "false").lower() == "true"
NOTIFY_ON_RECREATE_ERROR = os.environ.get("NOTIFY_ON_RECREATE_ERROR", "true").lower() == "true"
NOTIFY_ON_BULK_COMPLETE = os.environ.get("NOTIFY_ON_BULK_COMPLETE", "true").lower() == "true"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
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
check_results: dict = {}
last_full_check: Optional[str] = None
operations_log: list = []
jobs_state: dict = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def log_op(action, target, status, message):
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action, "target": target,
        "status": status, "message": message
    }
    with state_lock:
        operations_log.insert(0, entry)
        if len(operations_log) > 200:
            operations_log.pop()

def derive_stack_name(compose_path: str) -> str:
    p = Path(compose_path)
    return p.parent.name or "default"


def create_job(job_type: str, target: str, stack: Optional[str] = None,
               total_steps: int = 1, meta: Optional[dict] = None) -> str:
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
        "events": []
    }
    with state_lock:
        jobs_state[job_id] = job
        _trim_jobs_locked()
    return job_id


def update_job(job_id: str, progress: Optional[int] = None,
               current_step: Optional[str] = None,
               message: Optional[str] = None,
               event: Optional[dict] = None,
               status: Optional[str] = None):
    with state_lock:
        job = jobs_state.get(job_id)
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
                **event
            }
            job["events"].insert(0, entry)
            if len(job["events"]) > 100:
                job["events"].pop()


def finish_job(job_id: str, status: str = "success", message: str = ""):
    with state_lock:
        job = jobs_state.get(job_id)
        if not job:
            return
        job["status"] = status
        job["progress"] = job["total_steps"]
        job["message"] = message or job.get("message", "")
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["events"].insert(0, {
            "time": job["finished_at"],
            "status": status,
            "message": job["message"] or f"Job finished with status: {status}"
        })
        if len(job["events"]) > 100:
            job["events"].pop()
        _trim_jobs_locked()


def _trim_jobs_locked():
    if len(jobs_state) <= 100:
        return
    ordered = sorted(
        jobs_state.items(),
        key=lambda kv: kv[1].get("started_at", ""),
        reverse=True
    )
    keep_ids = {job_id for job_id, _ in ordered[:100]}
    for job_id in list(jobs_state.keys()):
        if job_id not in keep_ids:
            jobs_state.pop(job_id, None)


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
        # Load .env file from same directory if present
        env = {}
        env_file = Path(path).parent / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        env[k.strip()] = v.strip().strip('"').strip("'")

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

            images.append(img)

        return list(set(images))
    except Exception as e:
        log.warning(f"Failed to parse {path}: {e}")
        return []

def get_services_for_image(compose_path: str, image_ref: str) -> list[str]:
    try:
        env = {}
        env_file = Path(compose_path).parent / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        env[k.strip()] = v.strip().strip('"').strip("'")

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
        if registry in ("registry-1.docker.io", "docker.io"):
            # Official single-name images (redis, nginx, etc.) need library/ prefix
            if '/' not in repo:
                repo = f"library/{repo}"
            r = requests.get(
                f"https://auth.docker.io/token"
                f"?service=registry.docker.io&scope=repository:{repo}:pull",
                timeout=15,
            )
            r.raise_for_status()
            token = r.json().get("token")
            headers = {"Authorization": f"Bearer {token}", "Accept": accept}
            url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"

        elif registry == "ghcr.io":
            # GHCR anonymous token auth
            r = requests.get(
                f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull",
                timeout=15,
            )
            r.raise_for_status()
            token = r.json().get("token")
            headers = {"Authorization": f"Bearer {token}", "Accept": accept}
            url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"

        else:
            # Generic registry fallback (Quay, private registries, etc.)
            headers = {"Accept": accept}
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
    total_images = max(len(images), 1)

    update_job(job_id, progress=2,
               current_step="Checking image digests",
               message=f"Checking {len(images)} images")

    for idx, (img, paths) in enumerate(images, start=1):
        result = check_image(img)
        result["compose_files"] = paths
        result["stacks"] = sorted(list({derive_stack_name(p) for p in paths}))
        results[img] = result

        if idx == 1 or idx == total_images or idx % 5 == 0:
            update_job(
                job_id,
                progress=2,
                current_step=f"Checking image digests ({idx}/{total_images})",
                message=f"Checked {idx} of {total_images} images",
                event={"status": "info", "message": f"Checked {img}: {result['status']}"}
            )

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
                               status: str = "info", extra: Optional[dict] = None) -> dict:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "title": title,
        "message": message,
        "status": status,
        "host": os.uname().nodename,
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

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
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

@app.route("/api/images")
def api_images():
    with state_lock:
        return jsonify(list(check_results.values()))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route("/api/check", methods=["POST"])
def api_check():
    job_id = create_job("full_check", "all", total_steps=4)
    threading.Thread(target=run_full_check, args=(job_id,), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/api/check/<path:image_ref>", methods=["POST"])
def api_check_single(image_ref):
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


@app.route("/api/update/<path:image_ref>", methods=["POST"])
def api_update_image(image_ref):
    data = request.json or {}
    auto_recreate = data.get("auto_recreate")
    if auto_recreate is None:
        auto_recreate = AUTO_RECREATE_AFTER_PULL

    stack = None
    with state_lock:
        existing = check_results.get(image_ref, {})
        compose_files = existing.get("compose_files", []) or []
        stacks = sorted(list({derive_stack_name(p) for p in compose_files}))
        if stacks:
            stack = stacks[0]

    job_id = create_job(
        "pull_image",
        image_ref,
        stack=stack,
        total_steps=4 if auto_recreate else 3,
        meta={"image": image_ref, "compose_files": compose_files, "auto_recreate": auto_recreate}
    )

    log_op("pull", image_ref, "started", f"Pulling {image_ref}")
    update_job(job_id, progress=0, current_step="Pulling image", message=f"Pulling {image_ref}")

    try:
        if not docker_client:
            raise RuntimeError("Docker socket not connected")

        update_job(job_id, progress=1,
                   current_step="Downloading image",
                   message=f"Downloading {image_ref}",
                   event={"status": "started", "message": f"Pull started for {image_ref}"})

        docker_client.images.pull(image_ref)

        update_job(job_id, progress=2,
                   current_step="Refreshing status",
                   message=f"Refreshing status for {image_ref}",
                   event={"status": "info", "message": f"Pull finished for {image_ref}"})

        result = refresh_image_result(image_ref)

        notify_pull_result(
            image_ref,
            ok=True,
            message="Image pulled successfully",
            stacks=result.get("stacks", [])
        )

        if auto_recreate and result.get("compose_files"):
            update_job(job_id, progress=3,
                       current_step="Auto-recreating affected services",
                       message=f"Processing {len(result.get('compose_files', []))} compose file(s)",
                       event={"status": "started", "message": "Starting auto-recreate phase"})

            for compose_path in result.get("compose_files", []):
                services = get_services_for_image(compose_path, image_ref)
                stack_name = derive_stack_name(compose_path)
                try:
                    rr = recreate_compose(compose_path, services=services or None)
                    if rr.returncode == 0:
                        log_op("auto_recreate", compose_path, "success", rr.stdout or "Done")
                        update_job(job_id, event={
                            "status": "success",
                            "message": f"Recreated {compose_path} ({', '.join(services) if services else 'full stack'})"
                        })
                        notify_recreate_result(
                            compose_path,
                            ok=True,
                            message=rr.stdout or "Recreate completed",
                            stack=stack_name
                        )
                    else:
                        log_op("auto_recreate", compose_path, "error", rr.stderr)
                        update_job(job_id, event={
                            "status": "error",
                            "message": f"Recreate failed for {compose_path}: {rr.stderr}"
                        })
                        notify_recreate_result(
                            compose_path,
                            ok=False,
                            message=rr.stderr,
                            stack=stack_name
                        )
                except Exception as e:
                    log_op("auto_recreate", compose_path, "error", str(e))
                    update_job(job_id, event={
                        "status": "error",
                        "message": f"Recreate exception for {compose_path}: {e}"
                    })
                    notify_recreate_result(
                        compose_path,
                        ok=False,
                        message=str(e),
                        stack=stack_name
                    )

            result = refresh_image_result(image_ref)

        log_op("pull", image_ref, "success", "Pulled successfully")
        finish_job(
            job_id,
            "success",
            f"Pulled {image_ref} successfully" + (" with auto-recreate" if auto_recreate else "")
        )
        return jsonify({"status": "success", "result": result, "job_id": job_id})
    except Exception as e:
        log_op("pull", image_ref, "error", str(e))
        notify_pull_result(image_ref, ok=False, message=str(e), stacks=stacks)
        finish_job(job_id, "error", str(e))
        return jsonify({"status": "error", "message": str(e), "job_id": job_id}), 500

@app.route("/api/bulk/update", methods=["POST"])
def api_bulk_update():
    data = request.json or {}
    stack_name = data.get("stack")
    auto_recreate = data.get("auto_recreate")
    if auto_recreate is None:
        auto_recreate = AUTO_RECREATE_AFTER_PULL

    target = stack_name or "all"
    job_id = create_job(
        "bulk_pull",
        target,
        stack=stack_name,
        total_steps=1,
        meta={"stack": stack_name, "auto_recreate": auto_recreate}
    )

    threading.Thread(
        target=run_bulk_pull,
        args=(job_id, stack_name, auto_recreate),
        daemon=True
    ).start()

    return jsonify({
        "status": "started",
        "job_id": job_id,
        "stack": stack_name,
        "auto_recreate": auto_recreate
    })

@app.route("/api/prune/containers", methods=["POST"])
def api_prune_containers():
    job_id = create_job(
        "prune_containers",
        "containers",
        total_steps=2,
        meta={"prune_type": "containers"}
    )

    threading.Thread(
        target=run_prune_job,
        args=(job_id, "containers", False),
        daemon=True
    ).start()

    return jsonify({"status": "started", "job_id": job_id, "prune_type": "containers"})


@app.route("/api/prune/images", methods=["POST"])
def api_prune_images():
    data = request.json or {}
    include_all = bool(data.get("all", False))

    job_id = create_job(
        "prune_images",
        "images",
        total_steps=2,
        meta={"prune_type": "images", "all": include_all}
    )

    threading.Thread(
        target=run_prune_job,
        args=(job_id, "images", include_all),
        daemon=True
    ).start()

    return jsonify({
        "status": "started",
        "job_id": job_id,
        "prune_type": "images",
        "all": include_all
    })


@app.route("/api/prune/system", methods=["POST"])
def api_prune_system():
    job_id = create_job(
        "prune_system",
        "system",
        total_steps=2,
        meta={"prune_type": "system"}
    )

    threading.Thread(
        target=run_prune_job,
        args=(job_id, "system", False),
        daemon=True
    ).start()

    return jsonify({"status": "started", "job_id": job_id, "prune_type": "system"})


@app.route("/api/prune/volumes", methods=["POST"])
def api_prune_volumes():
    data = request.json or {}
    include_all = bool(data.get("all", False))

    job_id = create_job(
        "prune_volumes",
        "volumes",
        total_steps=2,
        meta={"prune_type": "volumes", "all": include_all}
    )

    threading.Thread(
        target=run_prune_job,
        args=(job_id, "volumes", include_all),
        daemon=True
    ).start()

    return jsonify({
        "status": "started",
        "job_id": job_id,
        "prune_type": "volumes",
        "all": include_all
    })

@app.route("/api/stacks/<stack_name>/recreate", methods=["POST"])
def api_stack_recreate(stack_name):
    job_id = create_job(
        "recreate_stack",
        stack_name,
        stack=stack_name,
        total_steps=1,
        meta={"stack": stack_name}
    )

    threading.Thread(
        target=run_stack_recreate,
        args=(job_id, stack_name),
        daemon=True
    ).start()

    return jsonify({"status": "started", "job_id": job_id, "stack": stack_name})

@app.route("/api/compose/recreate", methods=["POST"])
def api_compose_recreate():
    data = request.json or {}
    compose_path = data.get("compose_path")
    if not compose_path:
        return jsonify({"status": "error", "message": "compose_path required"}), 400

    compose_file = Path(compose_path)
    if not compose_file.exists():
        return jsonify({"status": "error", "message": "File not found"}), 404

    stack = derive_stack_name(str(compose_file))
    job_id = create_job(
        "recreate_stack",
        compose_path,
        stack=stack,
        total_steps=3,
        meta={"compose_path": compose_path}
    )

    log_op("recreate", compose_path, "started", "Running docker compose up -d")
    update_job(job_id, progress=0, current_step="Preparing recreate",
               message=f"Preparing recreate for {compose_path}")

    try:
        update_job(job_id, progress=1, current_step="Running docker compose",
                   message=f"docker compose up -d for {compose_path}",
                   event={"status": "started", "message": f"Recreate started for stack {stack}"})

        r = subprocess.run(
            ["docker", "compose", "-f", str(compose_file),
             "up", "-d", "--remove-orphans"],
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
            notify_recreate_result(
                compose_path,
                ok=True,
                message=r.stdout or "Recreate completed",
                stack=stack
            )
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


@app.route("/api/compose/files")
def api_compose_files():
    return jsonify(find_compose_files())


@app.route("/api/operations")
def api_operations():
    with state_lock:
        return jsonify(operations_log[:50])

@app.route("/api/stacks")
def api_stacks():
    return jsonify(summarize_stacks())


@app.route("/api/jobs")
def api_jobs():
    with state_lock:
        jobs = sorted(
            jobs_state.values(),
            key=lambda j: j.get("started_at", ""),
            reverse=True
        )
        return jsonify(jobs[:30])


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    with state_lock:
        job = jobs_state.get(job_id)
        if not job:
            return jsonify({"status": "error", "message": "Job not found"}), 404
        return jsonify(job)

@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
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
