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

# ── Config ────────────────────────────────────────────────────────────────────
COMPOSE_ROOT = os.environ.get("COMPOSE_ROOT", "/compose")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

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

    own_job = False
    if not job_id:
        job_id = create_job("full_check", "all", total_steps=4)
        own_job = True

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
            event={
                "status": "info",
                "message": f"Parsing compose file {cf['path']}"
            }
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
                event={
                    "status": "info",
                    "message": f"Checked {img}: {result['status']}"
                }
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

    finish_job(
        job_id,
        status="success",
        message=f"Checked {len(results)} images, {updates} updates available"
    )

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
            "check_interval_minutes": CHECK_INTERVAL_MINUTES
        })


@app.route("/api/images")
def api_images():
    with state_lock:
        return jsonify(list(check_results.values()))


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
        total_steps=3,
        meta={"image": image_ref, "compose_files": compose_files}
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

        result = check_image(image_ref)
        with state_lock:
            if image_ref in check_results:
                result["compose_files"] = check_results[image_ref].get("compose_files", [])
            else:
                result["compose_files"] = compose_files
            result["stacks"] = sorted(list({derive_stack_name(p) for p in result["compose_files"]}))
            check_results[image_ref] = result

        log_op("pull", image_ref, "success", "Pulled successfully")
        finish_job(job_id, "success", f"Pulled {image_ref} successfully")
        return jsonify({"status": "success", "result": result, "job_id": job_id})
    except Exception as e:
        log_op("pull", image_ref, "error", str(e))
        finish_job(job_id, "error", str(e))
        return jsonify({"status": "error", "message": str(e), "job_id": job_id}), 500


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
            finish_job(job_id, "success", f"Recreated stack {stack}, refreshed {refreshed} images")
            return jsonify({"status": "success", "output": r.stdout, "job_id": job_id})
        else:
            log_op("recreate", compose_path, "error", r.stderr)
            finish_job(job_id, "error", r.stderr)
            return jsonify({"status": "error", "message": r.stderr, "job_id": job_id}), 500
    except subprocess.TimeoutExpired:
        log_op("recreate", compose_path, "error", "Timed out")
        finish_job(job_id, "error", "Timed out after 300s")
        return jsonify({"status": "error", "message": "Timed out after 300s", "job_id": job_id}), 500
    except Exception as e:
        log_op("recreate", compose_path, "error", str(e))
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
