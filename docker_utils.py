import os
import time
import re
import json
import logging
import shutil
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
        resolved_path = resolve_compose_path(path)
        env = read_dotenv(resolved_path.parent / ".env")

        with open(resolved_path) as f:
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
        resolved_path = resolve_compose_path(compose_path)
        env = read_dotenv(resolved_path.parent / ".env")

        with open(resolved_path) as f:
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
                # Store path relative to COMPOSE_ROOT for proper API routing
                relative_path = str(full_path.relative_to(root))
                files.append({"path": relative_path, "project": Path(dirpath).name})
    
    return files


def recreate_compose(compose_path: str, services: Optional[list[str]] = None,
                     remove_orphans: bool = True, timeout: int = DEFAULT_COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
    compose_file = resolve_compose_path(compose_path)
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
        
        # Get live resource usage from running containers
        cpu_usage_percent = 0.0
        memory_used = 0
        running_containers = [c for c in containers if c.status == "running"]
        
        if running_containers:
            try:
                # Get stats for all running containers
                for container in running_containers:
                    try:
                        stats = container.stats(stream=False, one_shot=True)
                        if stats:
                            # CPU usage calculation
                            cpu_delta = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
                            system_cpu = stats.get("cpu_stats", {}).get("system_cpu_usage", 0)
                            cpu_cores = info.get("NCPU", 1)
                            if system_cpu > 0 and cpu_cores > 0:
                                cpu_percent = (cpu_delta / system_cpu) * 100.0 * cpu_cores
                                cpu_usage_percent += cpu_percent / len(running_containers)
                            
                            # Memory usage
                            memory_stats = stats.get("memory_stats", {})
                            usage = memory_stats.get("usage", 0)
                            memory_used += usage
                    except Exception:
                        continue
            except Exception as e:
                log.debug(f"Could not get container stats: {e}")
        
        # Calculate memory usage percentage
        memory_total = info.get("MemTotal", 0)
        memory_usage_percent = (memory_used / memory_total * 100) if memory_total > 0 else 0.0
        
        result = {
            "docker_version": info.get("DockerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "containers_total": info.get("Containers", 0),
            "images": info.get("Images", 0),
            "cpu_cores": info.get("NCPU", 0),
            "memory_total": info.get("MemTotal", 0),
            "memory_used": memory_used,
            "cpu_usage_percent": round(cpu_usage_percent, 1),
            "memory_usage_percent": round(memory_usage_percent, 1),
            "os": info.get("OperatingSystem", "unknown"),
            "architecture": info.get("Architecture", "unknown"),
            "kernel_version": info.get("KernelVersion", "unknown"),
            "server_version": info.get("ServerVersion", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Add disk usage
        disk_info = get_host_disk_usage()
        result.update(disk_info)
        
        return result
    except docker.errors.APIError as e:
        # Handle permission errors gracefully
        log.warning(f"Docker API error getting host resources: {e}")
        try:
            # Try to get basic info without the full info() call
            containers = client.containers.list()
            return {
                "error": "Limited access",
                "containers_running": len([c for c in containers if c.status == "running"]),
                "containers_stopped": len([c for c in containers if c.status != "running"]),
                "containers_total": len(containers),
                "images": "unknown",
                "cpu_cores": "unknown",
                "memory_total": 0,
                "memory_used": 0,
                "cpu_usage_percent": 0.0,
                "memory_usage_percent": 0.0,
                "os": "unknown",
                "architecture": "unknown", 
                "kernel_version": "unknown",
                "server_version": "unknown",
                "docker_version": "unknown",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        except Exception as e2:
            log.warning(f"Failed to get basic container info: {e2}")
            return {"error": str(e)}
    except Exception as e:
        log.warning(f"Failed to get host resources: {e}")
        return {"error": str(e)}


def get_host_disk_usage() -> dict:
    """Get disk usage information for the host.
    
    Returns:
        Dictionary with disk total, used, free, and usage percentage.
    """
    try:
        # Get disk usage for the root filesystem
        disk = shutil.disk_usage("/")
        total = disk.total
        used = disk.used
        free = disk.free
        usage_percent = (used / total * 100) if total > 0 else 0.0
        
        return {
            "disk_total": total,
            "disk_used": used,
            "disk_free": free,
            "disk_usage_percent": round(usage_percent, 1)
        }
    except Exception as e:
        log.debug(f"Could not get disk usage: {e}")
        return {
            "disk_total": 0,
            "disk_used": 0,
            "disk_free": 0,
            "disk_usage_percent": 0.0
        }


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


# -- Phase 2: Compose File Management Functions --


def get_compose_file_content(compose_path: str) -> Optional[dict]:
    """Read and parse a compose file, returning its content as a dictionary.
    
    Args:
        compose_path: Path to the compose file (absolute or relative to COMPOSE_ROOT)
        
    Returns:
        Parsed YAML content as dict, or None if file not found or invalid
    """
    try:
        path = Path(compose_path)
        
        # If path is relative, try to resolve it from COMPOSE_ROOT
        if not path.is_absolute():
            path = Path(COMPOSE_ROOT) / path
        
        if not path.exists():
            log.warning(f"Compose file not found: {compose_path} (resolved to: {path})")
            return None
        
        with open(path, 'r', encoding='utf-8') as f:
            content = yaml.safe_load(f)
        
        if content is None:
            return {}
        return content
    except yaml.YAMLError as e:
        log.warning(f"Invalid YAML in {compose_path}: {e}")
        return None
    except Exception as e:
        log.warning(f"Failed to read compose file {compose_path}: {e}")
        return None


def write_compose_file(compose_path: str, content: dict, backup: bool = True) -> tuple[bool, str]:
    """Write content to a compose file, optionally creating a backup first.
    
    Args:
        compose_path: Path to the compose file
        content: Dictionary to write as YAML
        backup: Whether to create a backup file before writing
        
    Returns:
        Tuple of (success, message)
    """
    try:
        path = resolve_compose_path(compose_path)
        
        # Create backup if requested
        if backup and path.exists():
            backup_path = path.with_suffix(path.suffix + '.bak')
            import shutil
            shutil.copy2(path, backup_path)
            log.info(f"Created backup of {compose_path} at {backup_path}")
        
        # Write new content
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(content, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        
        log.info(f"Successfully wrote compose file: {compose_path}")
        return True, f"Compose file saved successfully"
    except Exception as e:
        log.error(f"Failed to write compose file {compose_path}: {e}")
        return False, f"Failed to save compose file: {str(e)}"


def validate_compose_content(content: dict) -> tuple[bool, str, list]:
    """Validate compose file content structure.
    
    Args:
        content: Parsed YAML content to validate
        
    Returns:
        Tuple of (is_valid, message, errors_list)
    """
    errors = []
    
    if content is None:
        errors.append("Content is empty or None")
        return False, "Invalid: empty content", errors
    
    if not isinstance(content, dict):
        errors.append(f"Root must be a dictionary, got {type(content).__name__}")
        return False, "Invalid: root not a dictionary", errors
    
    # Check for valid top-level keys
    valid_top_level = {'version', 'services', 'networks', 'volumes', 'configs', 'secrets'}
    for key in content.keys():
        if key not in valid_top_level:
            errors.append(f"Unknown top-level key: {key}")
    
    # Check services if present
    services = content.get('services')
    if services is not None:
        if not isinstance(services, dict):
            errors.append("services must be a dictionary")
        else:
            for svc_name, svc_config in services.items():
                if not isinstance(svc_config, dict):
                    errors.append(f"Service '{svc_name}' configuration must be a dictionary")
    
    if errors:
        return False, f"Validation failed with {len(errors)} error(s)", errors
    
    return True, "Valid compose file", []


def get_compose_file_dependencies(compose_path: str) -> dict:
    """Extract dependency graph from a compose file.
    
    Analyzes service dependencies (depends_on), networks, volumes, and service links
    to build a dependency graph.
    
    Args:
        compose_path: Path to the compose file
        
    Returns:
        Dictionary with:
        - nodes: list of service names
        - edges: list of {from, to, type} tuples
        - networks: dict of network names to services
        - volumes: dict of volume names to services
    """
    content = get_compose_file_content(compose_path)
    if content is None:
        return {"nodes": [], "edges": [], "networks": {}, "volumes": {}}
    
    services = content.get('services') or {}
    networks_def = content.get('networks') or {}
    volumes_def = content.get('volumes') or {}
    
    nodes = list(services.keys())
    edges = []
    networks = {name: [] for name in networks_def.keys()}
    volumes_map = {name: [] for name in volumes_def.keys()}
    
    # Extract dependencies from depends_on
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        
        # depends_on
        depends_on = svc_config.get('depends_on')
        if depends_on:
            if isinstance(depends_on, list):
                for dep in depends_on:
                    if isinstance(dep, str):
                        edges.append({"from": dep, "to": svc_name, "type": "depends_on"})
                    elif isinstance(dep, dict):
                        # depends_on with condition
                        for condition_dep in dep.get('condition', []):
                            if isinstance(condition_dep, str):
                                edges.append({"from": condition_dep, "to": svc_name, "type": "depends_on"})
            elif isinstance(depends_on, dict):
                for dep, _ in depends_on.items():
                    edges.append({"from": dep, "to": svc_name, "type": "depends_on"})
        
        # networks
        svc_networks = svc_config.get('networks')
        if svc_networks:
            for net in svc_networks:
                if net in networks:
                    networks[net].append(svc_name)
        
        # volumes
        svc_volumes = svc_config.get('volumes')
        if svc_volumes:
            for vol in svc_volumes:
                if isinstance(vol, str):
                    vol_name = vol.split(':')[0]
                    if vol_name in volumes_map:
                        volumes_map[vol_name].append(svc_name)
    
    return {
        "nodes": nodes,
        "edges": edges,
        "networks": networks,
        "volumes": volumes_map
    }


def list_compose_files_detailed() -> list[dict]:
    """List all compose files with additional metadata.
    
    Returns:
        List of dictionaries with compose file info including:
        - path: full path to the file
        - project: project name (directory name)
        - filename: just the filename
        - services: list of service names (from parsing)
        - service_count: number of services
        - images: list of images used
        - image_count: number of unique images
    """
    files = find_compose_files()
    result = []
    
    for file_info in files:
        compose_path = file_info.get('path', '')
        project = file_info.get('project', '')
        filename = Path(compose_path).name
        
        content = get_compose_file_content(compose_path)
        services = []
        images = []
        
        if content:
            services_data = content.get('services') or {}
            services = list(services_data.keys())
            
            # Extract images
            for svc_name, svc_config in services_data.items():
                if not isinstance(svc_config, dict):
                    continue
                img = svc_config.get('image')
                if img:
                    images.append(img)
        
        result.append({
            "path": compose_path,
            "project": project,
            "filename": filename,
            "services": services,
            "service_count": len(services),
            "images": list(set(images)),
            "image_count": len(set(images))
        })
    
    return result


# -- Phase 2: Stack Management Functions --


def resolve_compose_path(compose_path: str) -> Path:
    """Resolve a compose file path to an absolute path.
    
    Args:
        compose_path: Path to the compose file (absolute or relative to COMPOSE_ROOT)
        
    Returns:
        Absolute Path to the compose file
    """
    path = Path(compose_path)
    
    # If path is relative, try to resolve it from COMPOSE_ROOT
    if not path.is_absolute():
        path = Path(COMPOSE_ROOT) / path
    
    return path


def get_stack_name_from_path(compose_path: str) -> str:
    """Derive stack name from compose file path.
    
    Args:
        compose_path: Path to the compose file
        
    Returns:
        Stack name (directory name containing the compose file)
    """
    return Path(compose_path).parent.name


def stack_up(compose_path: str, timeout: int = DEFAULT_COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
    """Start a stack using docker compose up.
    
    Args:
        compose_path: Path to the compose file
        timeout: Timeout in seconds
        
    Returns:
        CompletedProcess with result
    """
    compose_file = resolve_compose_path(compose_path)
    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"]
    
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(compose_file.parent)
    )


def stack_down(compose_path: str, timeout: int = DEFAULT_COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
    """Stop a stack using docker compose down.
    
    Args:
        compose_path: Path to the compose file
        timeout: Timeout in seconds
        
    Returns:
        CompletedProcess with result
    """
    compose_file = resolve_compose_path(compose_path)
    cmd = ["docker", "compose", "-f", str(compose_file), "down"]
    
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(compose_file.parent)
    )


def stack_restart(compose_path: str, timeout: int = DEFAULT_COMPOSE_TIMEOUT) -> subprocess.CompletedProcess:
    """Restart a stack using docker compose restart.
    
    Args:
        compose_path: Path to the compose file
        timeout: Timeout in seconds
        
    Returns:
        CompletedProcess with result
    """
    compose_file = resolve_compose_path(compose_path)
    cmd = ["docker", "compose", "-f", str(compose_file), "restart"]
    
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(compose_file.parent)
    )


def stack_ps(compose_path: str) -> subprocess.CompletedProcess:
    """Get status of all containers in a stack.
    
    Args:
        compose_path: Path to the compose file
        
    Returns:
        CompletedProcess with result
    """
    compose_file = resolve_compose_path(compose_path)
    cmd = ["docker", "compose", "-f", str(compose_file), "ps", "--format", "json"]
    
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(compose_file.parent)
    )


def get_stack_containers(compose_path: str) -> list[dict]:
    """Get containers belonging to a specific stack.
    
    Args:
        compose_path: Path to the compose file
        
    Returns:
        List of container dictionaries for this stack
    """
    client = docker_client()
    if not client:
        return []
    
    stack_name = get_stack_name_from_path(compose_path)
    
    try:
        # Get all containers and filter by stack label
        all_containers = client.containers.list(all=True)
        stack_containers = []
        
        for container in all_containers:
            labels = container.attrs.get('Config', {}).get('Labels', {})
            # Check for com.docker.compose.project label
            project = labels.get('com.docker.compose.project')
            if project == stack_name:
                stack_containers.append({
                    "id": container.short_id,
                    "name": container.name,
                    "status": container.status,
                    "state": container.attrs.get("State", {}).get("Status", ""),
                    "image": container.attrs.get("Config", {}).get("Image", ""),
                    "service": labels.get('com.docker.compose.service', '')
                })
        
        return stack_containers
    except Exception as e:
        log.warning(f"Failed to get containers for stack {stack_name}: {e}")
        return []


def get_all_stacks() -> dict[str, dict]:
    """Get information about all stacks (grouped compose projects).
    
    Returns:
        Dictionary mapping stack names to stack info:
        - compose_files: list of compose file paths
        - services: list of service names
        - containers: list of container info
        - status: overall status (running/stopped/mixed)
    """
    compose_files = find_compose_files()
    stacks = {}
    
    for file_info in compose_files:
        compose_path = file_info.get('path', '')
        stack_name = get_stack_name_from_path(compose_path)
        
        if stack_name not in stacks:
            stacks[stack_name] = {
                "compose_files": [],
                "services": [],
                "containers": [],
                "status": "unknown"
            }
        
        stacks[stack_name]["compose_files"].append(compose_path)
        
        # Parse services from compose file
        content = get_compose_file_content(compose_path)
        if content:
            services = list(content.get('services', {}).keys())
            stacks[stack_name]["services"].extend(services)
    
    # Get container status for each stack
    client = docker_client()
    if client:
        try:
            all_containers = client.containers.list(all=True)
            for container in all_containers:
                labels = container.attrs.get('Config', {}).get('Labels', {})
                project = labels.get('com.docker.compose.project')
                if project in stacks:
                    stacks[project]["containers"].append({
                        "id": container.short_id,
                        "name": container.name,
                        "status": container.status,
                        "service": labels.get('com.docker.compose.service', '')
                    })
            
            # Determine overall status
            for stack_name, stack_info in stacks.items():
                containers = stack_info.get("containers", [])
                if not containers:
                    stack_info["status"] = "stopped"
                else:
                    running = sum(1 for c in containers if c["status"] == "running")
                    stopped = sum(1 for c in containers if c["status"] != "running")
                    if stopped == 0:
                        stack_info["status"] = "running"
                    elif running == 0:
                        stack_info["status"] = "stopped"
                    else:
                        stack_info["status"] = "mixed"
        except Exception as e:
            log.warning(f"Failed to get container status for stacks: {e}")
    
    return stacks


def check_for_self_update(current_version: str, repo: str = "robsterba/docker-update-checker") -> dict:
    """Check GitHub Releases API for newer version of the application.
    
    Args:
        current_version: Current application version (e.g., "0.2.0")
        repo: GitHub repository in format "owner/repo"
    
    Returns:
        Dictionary with update info:
        {
            "current_version": str,
            "latest_version": str or None,
            "update_available": bool,
            "release_url": str or None,
            "release_notes": str or None,
            "error": str or None
        }
    """
    import re as _re
    
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    
    try:
        # Set User-Agent to avoid 403 errors from GitHub
        headers = {
            "User-Agent": "docker-update-checker",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # Use a short timeout
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        release_data = response.json()
        latest_tag = release_data.get("tag_name", "")
        
        # Extract version from tag (e.g., "v0.3.0" -> "0.3.0")
        # Handle both "v1.2.3" and "1.2.3" formats
        version_match = _re.match(r"^v?(?P<version>\d+\.\d+\.\d+.*?)$", latest_tag, _re.IGNORECASE)
        if version_match:
            latest_version = version_match.group("version")
        else:
            latest_version = latest_tag
        
        # Compare versions using simple string comparison (works for semver-like versions)
        # For more robust comparison, we could use packaging.version, but that adds a dependency
        update_available = latest_version != current_version and latest_version
        
        # Get release notes (body) - truncate if too long
        release_notes = release_data.get("body", "")
        if release_notes and len(release_notes) > 500:
            release_notes = release_notes[:500] + "..."
        
        return {
            "current_version": current_version,
            "latest_version": latest_version if latest_version else None,
            "update_available": update_available,
            "release_url": release_data.get("html_url"),
            "release_notes": release_notes if release_notes else None,
            "published_at": release_data.get("published_at"),
            "error": None
        }
    except requests.exceptions.Timeout:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "error": "Request timeout"
        }
    except requests.exceptions.RequestException as e:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "error": f"Request failed: {str(e)}"
        }
    except Exception as e:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "release_notes": None,
            "error": str(e)
        }


def detect_os() -> dict:
    """Detect the host operating system.
    
    Returns:
        Dictionary with os type, version, and family.
        {
            "os": "Ubuntu",
            "version": "22.04",
            "family": "debian",
            "package_manager": "apt"
        }
    """
    try:
        # Try /etc/os-release first (modern Linux systems)
        with open("/etc/os-release", "r") as f:
            lines = f.readlines()
        
        os_info = {}
        for line in lines:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                os_info[key] = value.strip('"')
        
        name = os_info.get("NAME", "").lower()
        version = os_info.get("VERSION_ID", "").strip('"')
        id_like = os_info.get("ID_LIKE", "").lower()
        
        # Determine family and package manager
        if "ubuntu" in name or "ubuntu" in id_like:
            return {"os": "Ubuntu", "version": version, "family": "debian", "package_manager": "apt"}
        elif "debian" in name or "debian" in id_like:
            return {"os": "Debian", "version": version, "family": "debian", "package_manager": "apt"}
        elif "centos" in name or "rhel" in name or "fedora" in name:
            return {"os": name.title(), "version": version, "family": "redhat", "package_manager": "dnf"}
        elif "alpine" in name:
            return {"os": "Alpine", "version": version, "family": "alpine", "package_manager": "apk"}
        elif "arch" in name:
            return {"os": "Arch", "version": version, "family": "arch", "package_manager": "pacman"}
        else:
            # Fallback detection
            return {"os": name.title() if name else "Unknown", "version": version, "family": "unknown", "package_manager": None}
    except Exception:
        return {"os": "Unknown", "version": "", "family": "unknown", "package_manager": None}


def check_os_updates() -> dict:
    """Check for available OS package updates.
    
    This function first tries to read from a mounted JSON file (recommended).
    If the file doesn't exist or is outdated, it falls back to running
    package manager commands directly (requires root in container).
    
    For production deployments, use the host-level agent approach:
    1. Deploy scripts/os_update_agent.py on the host
    2. Run it via cron or systemd timer
    3. Mount /var/lib/docker-update-checker/os-updates.json into the container
    
    Returns:
        Dictionary with OS info and list of upgradable packages:
        {
            "os": "Ubuntu",
            "version": "22.04",
            "family": "debian",
            "package_manager": "apt",
            "updates_available": 5,
            "security_updates": 2,
            "packages": [
                {"name": "libssl3", "current": "3.0.2", "available": "3.0.7"},
                ...
            ],
            "last_checked": "2026-08-22T23:32:50Z",
            "error": null
        }
    """
    # Try to read from mounted JSON file first (recommended approach)
    mounted_file = "/var/lib/docker-update-checker/os-updates.json"
    file_age_limit = 3600  # 1 hour - if file is older, try direct check
    
    if os.path.exists(mounted_file):
        try:
            with open(mounted_file, 'r') as f:
                file_data = json.load(f)
            
            # Check if file is recent
            if file_data.get("last_checked"):
                last_checked = datetime.fromisoformat(file_data["last_checked"].replace('Z', '+00:00'))
                age = (datetime.now(timezone.utc) - last_checked).total_seconds()
                
                if age < file_age_limit:
                    # File is recent enough, use it
                    file_data["source"] = "mounted_file"
                    return file_data
        except Exception as e:
            log.debug(f"Could not read or parse mounted OS updates file: {e}")
    
    # Fall back to direct check (requires package manager access in container)
    os_info = detect_os()
    package_manager = os_info.get("package_manager")
    
    result = {
        "os": os_info.get("os", "Unknown"),
        "version": os_info.get("version", ""),
        "family": os_info.get("family", "unknown"),
        "package_manager": package_manager,
        "updates_available": 0,
        "security_updates": 0,
        "packages": [],
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "source": "direct_check"
    }
    
    if not package_manager:
        result["error"] = "Unsupported OS or no package manager detected"
        return result
    
    try:
        packages = []
        
        if package_manager == "apt":
            # Debian/Ubuntu: Use apt list --upgradable
            # Note: This requires the container to have access to run apt
            # In production, this should be run on the host, not in the container
            try:
                # Update package lists first
                subprocess.run(["apt-get", "update", "-qq"], timeout=60, check=False)
                
                cmd = ["apt", "list", "--upgradable", "2>/dev/null"]
                output = subprocess.check_output(cmd, timeout=30, text=True)
                
                for line in output.strip().split('\n'):
                    if line and not line.startswith("Listing"):
                        # Parse format: package/arch new_version arch [upgradable from: old_version]
                        # Example: console-setup-linux/noble-updates 1.226ubuntu1.1 all [upgradable from: 1.226ubuntu1]
                        parts = line.split()
                        if len(parts) >= 4 and parts[0].count('/') > 0:
                            # Package name with architecture
                            pkg_full = parts[0]
                            pkg_name = pkg_full.split('/')[0]
                            new_version = parts[1]
                            
                            # Extract old version from [upgradable from: old_version]
                            old_version = "unknown"
                            if len(parts) >= 6 and parts[5].startswith('['):
                                # Parse: [upgradable from: old_version]
                                old_version = parts[5].split(':')[1].rstrip(']')
                            
                            # Check if it's a security update
                            is_security = "security" in line.lower()
                            
                            packages.append({
                                "name": pkg_name,
                                "current": old_version,
                                "available": new_version,
                                "security": is_security
                            })
                        
                result["updates_available"] = len(packages)
                result["security_updates"] = len([p for p in packages if p.get("security")])
                result["packages"] = packages
                
            except subprocess.TimeoutExpired:
                result["error"] = "Command timed out. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except FileNotFoundError:
                result["error"] = "apt-get not found. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except Exception as e:
                result["error"] = f"apt check failed: {str(e)}. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
        
        elif package_manager == "dnf":
            # RHEL/CentOS/Fedora: Use dnf check-update
            try:
                cmd = ["dnf", "check-update", "-q"]
                output = subprocess.check_output(cmd, timeout=30, text=True)
                
                for line in output.strip().split('\n'):
                    if line and not line.startswith("Last metadata") and '.' in line:
                        # Parse dnf output: package.arch  current->available  repo
                        parts = line.split()
                        if len(parts) >= 3:
                            pkg_info = parts[0]
                            pkg_name = pkg_info.split('.')[0]  # Remove .arch
                            version_info = parts[1]
                            versions = version_info.split('->')
                            current = versions[0] if len(versions) > 0 else "unknown"
                            available = versions[1] if len(versions) > 1 else "unknown"
                            
                            packages.append({
                                "name": pkg_name,
                                "current": current,
                                "available": available,
                                "security": "security" in line.lower() or "update" in line.lower()
                            })
                
                result["updates_available"] = len(packages)
                result["security_updates"] = len([p for p in packages if p.get("security")])
                result["packages"] = packages
                
            except subprocess.TimeoutExpired:
                result["error"] = "Command timed out. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except FileNotFoundError:
                result["error"] = "dnf not found. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except Exception as e:
                result["error"] = f"dnf check failed: {str(e)}. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
        
        elif package_manager == "apk":
            # Alpine: Use apk list --upgradable
            try:
                cmd = ["apk", "list", "--upgradable"]
                output = subprocess.check_output(cmd, timeout=30, text=True)
                
                for line in output.strip().split('\n'):
                    if line and len(line.split()) >= 2:
                        parts = line.split()
                        pkg_name = parts[0]
                        current = parts[1].split('-')[0]  # Remove version suffix
                        available = parts[1] if len(parts) > 1 else "unknown"
                        
                        packages.append({
                            "name": pkg_name,
                            "current": current,
                            "available": available,
                            "security": "security" in line.lower()
                        })
                
                result["updates_available"] = len(packages)
                result["security_updates"] = len([p for p in packages if p.get("security")])
                result["packages"] = packages
                
            except subprocess.TimeoutExpired:
                result["error"] = "Command timed out. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except FileNotFoundError:
                result["error"] = "apk not found. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except Exception as e:
                result["error"] = f"apk check failed: {str(e)}. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
        
        elif package_manager == "pacman":
            # Arch: Use pacman -Qu
            try:
                cmd = ["pacman", "-Qu"]
                output = subprocess.check_output(cmd, timeout=30, text=True)
                
                for line in output.strip().split('\n'):
                    if line:
                        # pacman output: old_version -> new_version  package_name
                        parts = line.split()
                        if len(parts) >= 3 and "->" in parts[0]:
                            versions = parts[0].split("->")
                            current = versions[0].strip()
                            available = versions[1].strip()
                            pkg_name = parts[2]
                            
                            packages.append({
                                "name": pkg_name,
                                "current": current,
                                "available": available,
                                "security": False  # pacman doesn't indicate security by default
                            })
                
                result["updates_available"] = len(packages)
                result["packages"] = packages
                
            except subprocess.TimeoutExpired:
                result["error"] = "Command timed out. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except FileNotFoundError:
                result["error"] = "pacman not found. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
            except Exception as e:
                result["error"] = f"pacman check failed: {str(e)}. For production, deploy the host-level agent (scripts/os_update_agent.py)"
                return result
        
        else:
            result["error"] = f"Unsupported package manager: {package_manager}. For production, deploy the host-level agent (scripts/os_update_agent.py)"
            return result
    
    except Exception as e:
        result["error"] = str(e)
        return result
    
    return result
