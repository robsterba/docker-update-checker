import os
import json
import logging
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import re
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


def run_full_check():
    global last_full_check
    log.info("Running full image check...")
    compose_files = find_compose_files()
    all_images: dict[str, list[str]] = {}

    for cf in compose_files:
        for img in parse_images_from_compose(cf["path"]):
            all_images.setdefault(img, []).append(cf["path"])

    results = {}
    for img, paths in all_images.items():
        result = check_image(img)
        result["compose_files"] = paths
        results[img] = result

    with state_lock:
        check_results.clear()
        check_results.update(results)
        last_full_check = datetime.now(timezone.utc).isoformat()

    updates = sum(1 for r in results.values() if r["status"] == "update_available")
    log.info(f"Check complete: {len(results)} images, {updates} updates available.")
    log_op("check", "all", "success",
           f"Checked {len(results)} images, {updates} updates available")

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
    threading.Thread(target=run_full_check, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/check/<path:image_ref>", methods=["POST"])
def api_check_single(image_ref):
    result = check_image(image_ref)
    with state_lock:
        if image_ref in check_results:
            result["compose_files"] = check_results[image_ref].get("compose_files", [])
        check_results[image_ref] = result
    log_op("check", image_ref, "success", f"Status: {result['status']}")
    return jsonify(result)


@app.route("/api/update/<path:image_ref>", methods=["POST"])
def api_update_image(image_ref):
    log_op("pull", image_ref, "started", f"Pulling {image_ref}")
    try:
        if not docker_client:
            raise RuntimeError("Docker socket not connected")
        docker_client.images.pull(image_ref)
        result = check_image(image_ref)
        with state_lock:
            if image_ref in check_results:
                result["compose_files"] = check_results[image_ref].get("compose_files", [])
            check_results[image_ref] = result
        log_op("pull", image_ref, "success", "Pulled successfully")
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        log_op("pull", image_ref, "error", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/compose/recreate", methods=["POST"])
def api_compose_recreate():
    data = request.json or {}
    compose_path = data.get("compose_path")
    if not compose_path:
        return jsonify({"status": "error", "message": "compose_path required"}), 400

    compose_file = Path(compose_path)
    if not compose_file.exists():
        return jsonify({"status": "error", "message": "File not found"}), 404

    log_op("recreate", compose_path, "started", "Running docker compose up -d")
    try:
        r = subprocess.run(
            ["docker", "compose", "-f", str(compose_file),
             "up", "-d", "--remove-orphans"],
            capture_output=True, text=True, timeout=300,
            cwd=str(compose_file.parent)
        )
        if r.returncode == 0:
            log_op("recreate", compose_path, "success", r.stdout or "Done")
            return jsonify({"status": "success", "output": r.stdout})
        else:
            log_op("recreate", compose_path, "error", r.stderr)
            return jsonify({"status": "error", "message": r.stderr}), 500
    except subprocess.TimeoutExpired:
        log_op("recreate", compose_path, "error", "Timed out")
        return jsonify({"status": "error", "message": "Timed out after 300s"}), 500
    except Exception as e:
        log_op("recreate", compose_path, "error", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/compose/files")
def api_compose_files():
    return jsonify(find_compose_files())


@app.route("/api/operations")
def api_operations():
    with state_lock:
        return jsonify(operations_log[:50])

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(run_full_check, "interval",
                  minutes=CHECK_INTERVAL_MINUTES, id="full_check")
scheduler.start()

threading.Thread(target=run_full_check, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
