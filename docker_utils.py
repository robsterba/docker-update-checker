import os
import time
import re
import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
import requests
import yaml

from config import (
    REGISTRY_TOKEN_CACHE,
    TOKEN_CACHE_TTL,
    COMPOSE_ROOT,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    STATUS_REGISTRY_ERROR,
    STATUS_NOT_PULLED,
    STATUS_UNKNOWN,
    DEFAULT_COMPOSE_TIMEOUT,
    DEFAULT_REGISTRY_TIMEOUT,
    REGISTRY_DELAY_SECONDS,
)

log = logging.getLogger(__name__)

# Rate limiting lock and tracking
_registry_lock = threading.Lock()
_last_registry_request = 0.0


def _rate_limit_registry():
    """Apply rate limiting between registry API requests."""
    global _last_registry_request
    if REGISTRY_DELAY_SECONDS > 0:
        elapsed = time.time() - _last_registry_request
        if elapsed < REGISTRY_DELAY_SECONDS:
            time.sleep(REGISTRY_DELAY_SECONDS - elapsed)
        with _registry_lock:
            _last_registry_request = time.time()


_docker_client: Optional[docker.DockerClient] = None


def get_docker_client() -> Optional[docker.DockerClient]:
    """Lazily initialize and return Docker client with reconnection support.
    
    This allows the Docker client to be reinitialized if the Docker daemon
    restarts, without requiring the application to be restarted.
    
    Returns:
        Docker client instance, or None if connection fails
    """
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
            _docker_client.ping()
            log.info("Docker socket connected.")
        except Exception as e:
            log.warning(f"Docker socket unavailable: {e}")
            _docker_client = None
    return _docker_client


# For backward compatibility, docker_client is now the function itself
# Call docker_client() to get the client instance
docker_client = get_docker_client


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
    """Find all compose files, without following symlinks for security."""
    root = Path(COMPOSE_ROOT)
    files = []
    patterns = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    
    # Use os.walk with followlinks=False for cross-version compatibility
    # (recurse_symlinks was added in Python 3.12, but we need to support older versions)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            if filename in patterns:
                full_path = Path(dirpath) / filename
                files.append({"path": str(full_path), "project": Path(dirpath).name})
    
    return files


def recreate_compose(compose_path: str, services: Optional[list[str]] = None,
                     remove_orphans: bool = True, timeout: int = DEFAULT_COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
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

    # Apply rate limiting before making registry API call
    _rate_limit_registry()

    token = None
    try:
        if registry in ("registry-1.docker.io", "docker.io"):
            if '/' not in repo:
                repo = f"library/{repo}"
            r = requests.get(
                "https://auth.docker.io/token",
                params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
                timeout=DEFAULT_REGISTRY_TIMEOUT,
            )
            # Handle 401/403 errors for Docker Hub rate limiting or auth issues
            if r.status_code in (401, 403):
                log.warning(
                    f"Docker Hub authentication error for {repo}. "
                    f"Mount /root/.docker/config.json to authenticate. "
                    f"Status: {r.status_code}"
                )
                return None
            r.raise_for_status()
            token = r.json().get("token")
        elif registry == "ghcr.io":
            r = requests.get(
                "https://ghcr.io/token",
                params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"},
                timeout=DEFAULT_REGISTRY_TIMEOUT,
            )
            # Handle GitHub auth errors
            if r.status_code in (401, 403):
                log.warning(
                    f"GitHub Container Registry authentication error for {repo}. "
                    f"Status: {r.status_code}"
                )
                return None
            r.raise_for_status()
            token = r.json().get("token")
    except requests.exceptions.Timeout:
        log.warning(f"Token retrieval timeout for {registry}/{repo}")
        return None
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
    # Apply rate limiting before making registry API call
    _rate_limit_registry()

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

        r2 = requests.head(url, headers=headers, timeout=DEFAULT_REGISTRY_TIMEOUT)
        
        # Handle 401/403 errors gracefully
        if r2.status_code in (401, 403):
            log.warning(
                f"Docker Hub authentication error for {image_ref}. "
                f"Mount /root/.docker/config.json to authenticate."
            )
            return None
        
        r2.raise_for_status()
        return (
            r2.headers.get("Docker-Content-Digest")
            or r2.headers.get("Etag", "").strip('"')
        )
    except requests.exceptions.Timeout:
        log.warning(f"Remote digest timeout for {image_ref}")
        return None
    except Exception as e:
        log.warning(f"Remote digest failed for {image_ref}: {e}")
        return None


def get_local_digest(image_ref: str) -> Optional[str]:
    client = docker_client()
    if not client:
        return None
    try:
        img = client.images.get(image_ref)
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
        status = STATUS_UNKNOWN
    elif local is None:
        status = STATUS_NOT_PULLED
    elif remote is None:
        status = STATUS_REGISTRY_ERROR
    elif local == remote:
        status = STATUS_UP_TO_DATE
    else:
        status = STATUS_UPDATE_AVAILABLE

    return {
        "image": image_ref, "status": status,
        "local_digest": local, "remote_digest": remote,
        "checked_at": now
    }


# -- Container Management Functions --

def list_containers(all_containers: bool = False, filters: Optional[dict] = None) -> list[dict]:
    """List all containers with their basic information.
    
    Args:
        all_containers: If True, include stopped containers
        filters: Optional dictionary of filters (status, name, etc.)
        
    Returns:
        List of container dictionaries with id, name, status, etc.
    """
    client = docker_client()
    if not client:
        return []
    
    try:
        containers = client.containers.list(all=all_containers, filters=filters)
        return [{
            "id": c.short_id,
            "name": c.name,
            "status": c.status,
            "state": c.attrs.get("State", {}).get("Status", ""),
            "image": c.attrs.get("Config", {}).get("Image", ""),
            "created": c.attrs.get("Created", ""),
            "ports": c.attrs.get("NetworkSettings", {}).get("Ports", {}),
            "labels": c.attrs.get("Config", {}).get("Labels", {}),
            "health": c.attrs.get("State", {}).get("Health", None),
            "exit_code": c.attrs.get("State", {}).get("ExitCode", None),
            "is_running": c.status == "running",
        } for c in containers]
    except Exception as e:
        log.warning(f"Failed to list containers: {e}")
        return []


def inspect_container(container_id: str) -> Optional[dict]:
    """Get detailed information about a specific container.
    
    Args:
        container_id: Container ID or name
        
    Returns:
        Container inspection data or None if not found
    """
    client = docker_client()
    if not client:
        return None
    
    try:
        container = client.containers.get(container_id)
        return container.attrs
    except docker.errors.NotFound:
        return None
    except Exception as e:
        log.warning(f"Failed to inspect container {container_id}: {e}")
        return None


def get_container_resources(container_id: str) -> Optional[dict]:
    """Get resource usage statistics for a specific container.
    
    Args:
        container_id: Container ID or name
        
    Returns:
        Resource usage data or None if failed
    """
    client = docker_client()
    if not client:
        return None
    
    try:
        container = client.containers.get(container_id)
        # Get resource stats - use one_shot to get a single stats snapshot
        stats_generator = container.stats(stream=False, one_shot=True)
        
        # Handle both generator and direct return cases
        try:
            stats_data = next(stats_generator)
        except (StopIteration, TypeError):
            # If it's not a generator, it might be the direct stats dict
            stats_data = stats_generator
        
        # Parse if bytes
        if isinstance(stats_data, bytes):
            import json
            stats_data = json.loads(stats_data.decode('utf-8'))
        
        # Handle case where stats might be a generator that needs to be consumed
        if hasattr(stats_data, '__iter__') and not isinstance(stats_data, (dict, str)):
            stats_data = next(stats_data, {})
        
        # Extract key metrics
        cpu_stats = stats_data.get("cpu_stats", {})
        memory_stats = stats_data.get("memory_stats", {})
        
        # CPU usage calculation - improved
        cpu_usage_percent = None
        precpu_stats = stats_data.get("precpu_stats", {})
        
        if "cpu_usage" in cpu_stats and "precpu_stats" in stats_data:
            try:
                # Calculate CPU percentage using standard formula
                cpu_delta = cpu_stats["cpu_usage"]["total_usage"] - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
                system_cpu_delta = cpu_stats["system_cpu_usage"] - precpu_stats.get("system_cpu_usage", 0)
                
                if system_cpu_delta > 0 and cpu_delta > 0:
                    cpu_usage_percent = (cpu_delta / system_cpu_delta) * 100
                    # Cap at 100%
                    cpu_usage_percent = min(100, cpu_usage_percent)
            except (KeyError, TypeError, ZeroDivisionError):
                # Fallback: try simple calculation
                total_usage = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
                system_usage = cpu_stats.get("system_cpu_usage", 0)
                if system_usage > 0 and total_usage > 0:
                    cpu_usage_percent = min(100, (total_usage / system_usage) * 100)
        
        # Memory usage - more robust
        memory_usage = memory_stats.get("usage", 0)
        memory_limit = memory_stats.get("limit", 0)
        memory_percent = 0
        if memory_limit > 0:
            memory_percent = min(100, (memory_usage / memory_limit) * 100)
        
        return {
            "cpu_percent": round(cpu_usage_percent, 1) if cpu_usage_percent is not None else None,
            "memory_usage": memory_usage,
            "memory_limit": memory_limit,
            "memory_percent": round(memory_percent, 1),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        log.warning(f"Failed to get resources for container {container_id}: {e}")
        return None


def get_all_container_resources(containers: list[dict]) -> dict[str, Optional[dict]]:
    """Get resource usage for multiple containers efficiently.
    
    Args:
        containers: List of container dicts with 'id' field
        
    Returns:
        Dict mapping container_id to resource data
    """
    client = docker_client()
    if not client:
        return {}
    
    results = {}
    for container in containers:
        container_id = container.get("id", "")
        if container_id:
            results[container_id] = get_container_resources(container_id)
    
    return results


def get_host_resources() -> dict:
    """Get aggregate resource usage for the Docker host.
    
    Returns:
        Dictionary with total CPU, memory, container count, etc.
    """
    client = docker_client()
    if not client:
        return {"error": "Docker client not available"}
    
    try:
        info = client.info()
        
        # Get running container count
        containers = client.containers.list()
        
        return {
            "docker_version": info.get("DockerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "containers_total": info.get("Containers", 0),
            "images": info.get("Images", 0),
            "cpu_cores": info.get("NCPU", 0),
            "memory_total": info.get("MemTotal", 0),
            "os": info.get("OperatingSystem", "unknown"),
            "architecture": info.get("Architecture", "unknown"),
            "kernel_version": info.get("KernelVersion", "unknown"),
            "server_version": info.get("ServerVersion", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        log.warning(f"Failed to get host resources: {e}")
        return {"error": str(e)}


def start_container(container_id: str) -> tuple[bool, str]:
    """Start a stopped container.
    
    Args:
        container_id: Container ID or name
        
    Returns:
        Tuple of (success, message)
    """
    client = docker_client()
    if not client:
        return False, "Docker client not available"
    
    try:
        container = client.containers.get(container_id)
        container.start()
        return True, f"Container {container_id} started successfully"
    except docker.errors.NotFound:
        return False, f"Container {container_id} not found"
    except docker.errors.APIError as e:
        return False, f"Failed to start container: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def stop_container(container_id: str, timeout: int = 10) -> tuple[bool, str]:
    """Stop a running container.
    
    Args:
        container_id: Container ID or name
        timeout: Timeout in seconds before force kill
        
    Returns:
        Tuple of (success, message)
    """
    client = docker_client()
    if not client:
        return False, "Docker client not available"
    
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=timeout)
        return True, f"Container {container_id} stopped successfully"
    except docker.errors.NotFound:
        return False, f"Container {container_id} not found"
    except docker.errors.APIError as e:
        return False, f"Failed to stop container: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def restart_container(container_id: str, timeout: int = 10) -> tuple[bool, str]:
    """Restart a container.
    
    Args:
        container_id: Container ID or name
        timeout: Timeout in seconds before force kill
        
    Returns:
        Tuple of (success, message)
    """
    client = docker_client()
    if not client:
        return False, "Docker client not available"
    
    try:
        container = client.containers.get(container_id)
        container.restart(timeout=timeout)
        return True, f"Container {container_id} restarted successfully"
    except docker.errors.NotFound:
        return False, f"Container {container_id} not found"
    except docker.errors.APIError as e:
        return False, f"Failed to restart container: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"
