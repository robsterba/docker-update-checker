import time
import re
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

import docker
import requests
import yaml

from config import REGISTRY_TOKEN_CACHE, TOKEN_CACHE_TTL, COMPOSE_ROOT

log = logging.getLogger(__name__)


docker_client: Optional[docker.DockerClient] = None
try:
    docker_client = docker.from_env()
    docker_client.ping()
    log.info("Docker socket connected.")
except Exception as e:
    log.warning(f"Docker socket unavailable: {e}")


def read_dotenv(dotenv_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not dotenv_path.exists():
        return env

    with dotenv_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def resolve_env_vars(value: str, env: dict) -> str:
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

            if '${' in img:
                log.debug(f"Skipping unresolved image ref: {img}")
                continue

            if '@sha256:' in img:
                img = img.split('@')[0]

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


def find_compose_files() -> list[dict]:
    root = Path(COMPOSE_ROOT)
    files = []
    for pattern in ("docker-compose.yml", "docker-compose.yaml",
                    "compose.yml", "compose.yaml"):
        for p in root.rglob(pattern):
            files.append({"path": str(p), "project": p.parent.name})
    return files


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


def parse_image_ref(image_ref: str) -> tuple[str, str, str]:
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
