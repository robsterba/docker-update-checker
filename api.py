from flask import send_from_directory, jsonify, request, Response
import threading
from pathlib import Path
from typing import Any

# Import Flask app and configuration
from app import (
    app,
    docker_client,
    AUTO_RECREATE_AFTER_PULL,
    CHECK_INTERVAL_MINUTES,
    get_all_instances,
    proxy_local_request,
    proxy_remote_request,
    derive_stack_name,
    run_full_check,
    refresh_image_result,
    summarize_stacks,
    run_bulk_pull,
    run_stack_recreate,
    run_prune_job,
    load_notification_settings,
    save_notification_settings,
)

# Import from config module
from config import (
    get_env,
    get_bool_env,
    get_int_env,
)

# Import from canonical modules
from jobs import (
    state_lock,
    check_results,
    operations_log,
    jobs_state,
    log_op,
    create_job,
    update_job,
    finish_job,
    get_last_full_check,
)

from docker_utils import (
    check_image,
    find_compose_files,
    get_services_for_image,
    recreate_compose,
    list_containers,
    inspect_container,
    get_container_resources,
    get_all_container_resources,
    get_host_resources,
    start_container,
    stop_container,
    restart_container,
    # Phase 2: Compose file management
    get_compose_file_content,
    write_compose_file,
    validate_compose_content,
    get_compose_file_dependencies,
    list_compose_files_detailed,
    # Phase 2: Stack management
    get_stack_name_from_path,
    stack_up,
    stack_down,
    stack_restart,
    stack_ps,
    get_stack_containers,
    get_all_stacks,
    # Self-update checker
    check_for_self_update,
    # OS update checker
    check_os_updates,
)
from notifier import (
    send_notification,
    notify_pull_result,
    notify_recreate_result,
)
from config import NOTIFY_ENABLED, NOTIFY_BACKEND, DEFAULT_COMPOSE_TIMEOUT, VERSION, GITHUB_REPO, SELF_UPDATE_CHECK_ENABLED, OS_UPDATE_CHECK_ENABLED


# ── Routes (moved from app.py) ─────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    """Health check endpoint for monitoring."""
    docker_connected = docker_client() is not None
    return jsonify({
        "status": "ok",
        "docker_connected": docker_connected,
        "version": VERSION
    }), 200


@app.route("/api/version")
def api_version():
    """Get the application version."""
    return jsonify({"version": VERSION})


@app.route("/api/checker/updates")
def api_checker_updates():
    """Check if a newer version of the application is available."""
    update_info = check_for_self_update(VERSION, GITHUB_REPO)
    return jsonify(update_info)


@app.route("/api/checker/updates/check", methods=["POST"])
def api_checker_updates_check():
    """Trigger a check for application updates and return the result."""
    update_info = check_for_self_update(VERSION, GITHUB_REPO)
    
    # Optionally send notification if update is available and notifications are enabled
    if update_info.get("update_available") and NOTIFY_ENABLED and SELF_UPDATE_CHECK_ENABLED:
        try:
            send_notification(
                title="Application Update Available",
                message=f"docker-update-checker {update_info['latest_version']} is available (current: {update_info['current_version']})",
                event_type="self_update_available",
                data={
                    "current_version": update_info["current_version"],
                    "latest_version": update_info["latest_version"],
                    "release_url": update_info.get("release_url"),
                    "release_notes": update_info.get("release_notes", "")[:200]
                }
            )
        except Exception as e:
            log.warning(f"Failed to send self-update notification: {e}")
    
    return jsonify(update_info)


@app.route("/api/host/os-updates")
def api_host_os_updates():
    """Check for available OS package updates on the host."""
    if not OS_UPDATE_CHECK_ENABLED:
        return jsonify({"error": "OS update checking is disabled", "enabled": False})
    
    os_updates = check_os_updates()
    return jsonify(os_updates)


@app.route("/api/host/os-updates/check", methods=["POST"])
def api_host_os_updates_check():
    """Trigger a check for OS package updates and send notification if updates are available."""
    if not OS_UPDATE_CHECK_ENABLED:
        return jsonify({"error": "OS update checking is disabled", "enabled": False})
    
    os_updates = check_os_updates()
    
    # Send notification if updates are available and notifications are enabled
    if os_updates.get("updates_available", 0) > 0 and NOTIFY_ENABLED and OS_UPDATE_CHECK_ENABLED:
        try:
            packages_count = os_updates.get("updates_available", 0)
            security_count = os_updates.get("security_updates", 0)
            os_name = os_updates.get("os", "Unknown")
            
            send_notification(
                title=f"OS Updates Available on {os_name}",
                message=f"{packages_count} package(s) can be updated ({security_count} security updates)",
                event_type="os_updates_available",
                data={
                    "os": os_name,
                    "os_version": os_updates.get("version", ""),
                    "updates_available": packages_count,
                    "security_updates": security_count,
                    "package_manager": os_updates.get("package_manager"),
                    "packages": os_updates.get("packages", [])[:10]  # Limit to first 10
                }
            )
        except Exception as e:
            log.warning(f"Failed to send OS update notification: {e}")
    
    return jsonify(os_updates)


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "last_check": get_last_full_check(),
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
            "notify_backend": NOTIFY_BACKEND or None,
            "version": VERSION
        })


@app.route("/api/instances")
def api_instances():
    return jsonify(get_all_instances())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify({"auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL})

    from schemas import ConfigUpdateRequest
    
    data = request.get_json(silent=True) or {}
    try:
        # Validate using Pydantic model
        validated = ConfigUpdateRequest.model_validate(data)
        auto_recreate = validated.auto_recreate
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    # Update the module-level variable in config
    # Since all imports reference the same module, this updates it globally
    import config
    config.AUTO_RECREATE_AFTER_PULL = auto_recreate
    log_op("config", "auto_recreate", "success", f"Set auto_recreate_after_pull={auto_recreate}")
    return jsonify({"auto_recreate_after_pull": auto_recreate})


@app.route("/api/instances/<instance_id>/<path:proxy_path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_instance_proxy(instance_id, proxy_path):
    if instance_id == "local":
        return proxy_local_request(proxy_path)
    return proxy_remote_request(instance_id, proxy_path)


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
    from schemas import ImageUpdateRequest
    
    data = request.json or {}
    try:
        validated = ImageUpdateRequest.model_validate(data)
        auto_recreate = validated.auto_recreate
        if auto_recreate is None:
            auto_recreate = AUTO_RECREATE_AFTER_PULL
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "job_id": None}), 400

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
        client = docker_client()
        if not client:
            raise RuntimeError("Docker socket not connected")

        update_job(job_id, progress=1,
                   current_step="Downloading image",
                   message=f"Downloading {image_ref}",
                   event={"status": "started", "message": f"Pull started for {image_ref}"})

        client.images.pull(image_ref)

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
    from schemas import BulkUpdateRequest
    
    data = request.json or {}
    try:
        validated = BulkUpdateRequest.model_validate(data)
        stack_name = validated.stack
        auto_recreate = validated.auto_recreate
        if auto_recreate is None:
            auto_recreate = AUTO_RECREATE_AFTER_PULL
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

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
    from schemas import PruneRequest
    
    data = request.json or {}
    try:
        validated = PruneRequest.model_validate(data)
        include_all = validated.all
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

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
    from schemas import PruneRequest
    
    data = request.json or {}
    try:
        validated = PruneRequest.model_validate(data)
        include_all = validated.all
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

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
    from schemas import ComposeRecreateRequest
    
    data = request.json or {}
    try:
        validated = ComposeRecreateRequest.model_validate(data)
        compose_path = validated.compose_path
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

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
    return jsonify(operations_log.latest(50))


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


# -- Container Management Endpoints --

@app.route("/api/containers")
def api_containers():
    """List all containers with basic information and optional resource data."""
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


@app.route("/api/containers/<path:container_id>")
def api_container_inspect(container_id):
    """Get detailed information about a specific container."""
    container_data = inspect_container(container_id)
    if container_data is None:
        return jsonify({"status": "error", "message": "Container not found"}), 404
    return jsonify(container_data)


@app.route("/api/containers/<path:container_id>/resources")
def api_container_resources(container_id):
    """Get resource usage statistics for a specific container."""
    resources = get_container_resources(container_id)
    if resources is None:
        return jsonify({"status": "error", "message": "Container not found or unavailable"}), 404
    return jsonify(resources)


@app.route("/api/host/resources")
def api_host_resources():
    """Get aggregate resource usage for the Docker host."""
    resources = get_host_resources()
    return jsonify(resources)


@app.route("/api/containers/<path:container_id>/start", methods=["POST"])
def api_container_start(container_id):
    """Start a stopped container."""
    success, message = start_container(container_id)
    if success:
        log_op("container_start", container_id, "success", message)
        return jsonify({"status": "success", "message": message})
    else:
        log_op("container_start", container_id, "error", message)
        return jsonify({"status": "error", "message": message}), 400


@app.route("/api/containers/<path:container_id>/stop", methods=["POST"])
def api_container_stop(container_id):
    """Stop a running container."""
    timeout = request.args.get("timeout", 10, type=int)
    success, message = stop_container(container_id, timeout=timeout)
    if success:
        log_op("container_stop", container_id, "success", message)
        return jsonify({"status": "success", "message": message})
    else:
        log_op("container_stop", container_id, "error", message)
        return jsonify({"status": "error", "message": message}), 400


@app.route("/api/containers/<path:container_id>/restart", methods=["POST"])
def api_container_restart(container_id):
    """Restart a container."""
    timeout = request.args.get("timeout", 10, type=int)
    success, message = restart_container(container_id, timeout=timeout)
    if success:
        log_op("container_restart", container_id, "success", message)
        return jsonify({"status": "success", "message": message})
    else:
        log_op("container_restart", container_id, "error", message)
        return jsonify({"status": "error", "message": message}), 400


# -- Phase 2: Compose File Management Endpoints --


@app.route("/api/compose/files/detailed")
def api_compose_files_detailed():
    """List all compose files with detailed metadata (services, images, etc.)."""
    project_filter = request.args.get("project", None)
    files = list_compose_files_detailed()
    
    if project_filter:
        files = [f for f in files if f.get("project") == project_filter]
    
    return jsonify(files)


@app.route("/api/compose/files/<path:compose_path>")
def api_compose_file_get(compose_path):
    """Get the content of a specific compose file."""
    content = get_compose_file_content(compose_path)
    if content is None:
        return jsonify({"status": "error", "message": "File not found or invalid"}), 404
    
    return jsonify({"path": compose_path, "content": content})


@app.route("/api/compose/files/<path:compose_path>", methods=["PUT"])
def api_compose_file_update(compose_path):
    """Update/save a compose file with new content."""
    from schemas import ComposeFileContentRequest
    
    data = request.json or {}
    try:
        validated = ComposeFileContentRequest.model_validate(data)
        content = validated.content
        backup = validated.backup
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    
    # Validate content
    is_valid, validation_msg, errors = validate_compose_content(content)
    if not is_valid:
        return jsonify({
            "status": "error",
            "message": validation_msg,
            "errors": errors
        }), 400
    
    success, message = write_compose_file(compose_path, content, backup=backup)
    if success:
        log_op("compose_update", compose_path, "success", message)
        return jsonify({"status": "success", "message": message, "path": compose_path})
    else:
        log_op("compose_update", compose_path, "error", message)
        return jsonify({"status": "error", "message": message}), 500


@app.route("/api/compose/files/<path:compose_path>/validate", methods=["POST"])
def api_compose_file_validate(compose_path):
    """Validate a compose file's content."""
    from schemas import ComposeFileValidateRequest
    
    # Get content from request body or read from file
    data = request.json or {}
    content = data.get("content")
    
    if content is None:
        # Read from file if no content provided
        content = get_compose_file_content(compose_path)
        if content is None:
            return jsonify({"status": "error", "message": "File not found"}), 404
    
    is_valid, message, errors = validate_compose_content(content)
    return jsonify({
        "valid": is_valid,
        "message": message,
        "errors": errors
    })


@app.route("/api/compose/files/<path:compose_path>/dependencies")
def api_compose_file_dependencies(compose_path):
    """Get dependency graph for a compose file."""
    dependencies = get_compose_file_dependencies(compose_path)
    return jsonify({"path": compose_path, **dependencies})


# -- Phase 2: Stack Management Endpoints --


@app.route("/api/stacks/all")
def api_all_stacks():
    """Get information about all stacks (compose projects)."""
    stacks = get_all_stacks()
    return jsonify(stacks)


@app.route("/api/stacks/<stack_name>")
def api_stack_info(stack_name):
    """Get information about a specific stack."""
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    return jsonify({stack_name: stack_info})


@app.route("/api/stacks/<stack_name>/containers")
def api_stack_containers(stack_name):
    """Get containers for a specific stack."""
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    return jsonify({"stack": stack_name, "containers": stack_info.get("containers", [])})


@app.route("/api/stacks/<stack_name>/status")
def api_stack_status(stack_name):
    """Get status of all containers in a stack."""
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    return jsonify({
        "stack": stack_name,
        "status": stack_info.get("status", "unknown"),
        "containers": stack_info.get("containers", [])
    })


@app.route("/api/stacks/<stack_name>/up", methods=["POST"])
def api_stack_up(stack_name):
    """Start a stack (docker compose up -d)."""
    from schemas import StackActionRequest
    
    try:
        data = request.get_json(silent=True) or {}
        validated = StackActionRequest.model_validate(data)
        timeout = validated.timeout or DEFAULT_COMPOSE_TIMEOUT
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    
    # Find compose files for this stack
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    compose_files = stack_info.get("compose_files", [])
    if not compose_files:
        return jsonify({"status": "error", "message": f"No compose files found for stack '{stack_name}'"}), 400
    
    results = []
    all_success = True
    
    for compose_path in compose_files:
        try:
            result = stack_up(compose_path, timeout=timeout)
            results.append({
                "compose_file": compose_path,
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr
            })
            if result.returncode != 0:
                all_success = False
        except Exception as e:
            results.append({
                "compose_file": compose_path,
                "success": False,
                "output": "",
                "error": str(e)
            })
            all_success = False
    
    if all_success:
        log_op("stack_up", stack_name, "success", f"Started {len(compose_files)} compose file(s)")
        return jsonify({"status": "success", "stack": stack_name, "results": results})
    else:
        log_op("stack_up", stack_name, "error", "Partial or complete failure")
        return jsonify({"status": "partial_success", "stack": stack_name, "results": results}), 207


@app.route("/api/stacks/<stack_name>/down", methods=["POST"])
def api_stack_down(stack_name):
    """Stop a stack (docker compose down)."""
    from schemas import StackActionRequest
    
    try:
        data = request.get_json(silent=True) or {}
        validated = StackActionRequest.model_validate(data)
        timeout = validated.timeout or DEFAULT_COMPOSE_TIMEOUT
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    
    # Find compose files for this stack
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    compose_files = stack_info.get("compose_files", [])
    if not compose_files:
        return jsonify({"status": "error", "message": f"No compose files found for stack '{stack_name}'"}), 400
    
    results = []
    all_success = True
    
    for compose_path in compose_files:
        try:
            result = stack_down(compose_path, timeout=timeout)
            results.append({
                "compose_file": compose_path,
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr
            })
            if result.returncode != 0:
                all_success = False
        except Exception as e:
            results.append({
                "compose_file": compose_path,
                "success": False,
                "output": "",
                "error": str(e)
            })
            all_success = False
    
    if all_success:
        log_op("stack_down", stack_name, "success", f"Stopped {len(compose_files)} compose file(s)")
        return jsonify({"status": "success", "stack": stack_name, "results": results})
    else:
        log_op("stack_down", stack_name, "error", "Partial or complete failure")
        return jsonify({"status": "partial_success", "stack": stack_name, "results": results}), 207


@app.route("/api/stacks/<stack_name>/restart", methods=["POST"])
def api_stack_restart(stack_name):
    """Restart all containers in a stack."""
    from schemas import StackActionRequest
    
    try:
        data = request.get_json(silent=True) or {}
        validated = StackActionRequest.model_validate(data)
        timeout = validated.timeout or DEFAULT_COMPOSE_TIMEOUT
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    
    # Find compose files for this stack
    stacks = get_all_stacks()
    stack_info = stacks.get(stack_name)
    
    if stack_info is None:
        return jsonify({"status": "error", "message": f"Stack '{stack_name}' not found"}), 404
    
    compose_files = stack_info.get("compose_files", [])
    if not compose_files:
        return jsonify({"status": "error", "message": f"No compose files found for stack '{stack_name}'"}), 400
    
    results = []
    all_success = True
    
    for compose_path in compose_files:
        try:
            result = stack_restart(compose_path, timeout=timeout)
            results.append({
                "compose_file": compose_path,
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr
            })
            if result.returncode != 0:
                all_success = False
        except Exception as e:
            results.append({
                "compose_file": compose_path,
                "success": False,
                "output": "",
                "error": str(e)
            })
            all_success = False
    
    if all_success:
        log_op("stack_restart", stack_name, "success", f"Restarted {len(compose_files)} compose file(s)")
        return jsonify({"status": "success", "stack": stack_name, "results": results})
    else:
        log_op("stack_restart", stack_name, "error", "Partial or complete failure")
        return jsonify({"status": "partial_success", "stack": stack_name, "results": results}), 207


@app.route("/api/stacks/bulk", methods=["POST"])
def api_stacks_bulk_action():
    """Perform bulk action on multiple stacks."""
    from schemas import StackBulkActionRequest
    
    data = request.json or {}
    try:
        validated = StackBulkActionRequest.model_validate(data)
        stack_names = validated.stack_names
        action = validated.action
        timeout = validated.timeout or DEFAULT_COMPOSE_TIMEOUT
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    
    action_map = {
        'up': stack_up,
        'down': stack_down,
        'restart': stack_restart
    }
    
    if action not in action_map:
        return jsonify({"status": "error", "message": f"Invalid action: {action}"}), 400
    
    stacks = get_all_stacks()
    results = {}
    all_success = True
    
    for stack_name in stack_names:
        stack_info = stacks.get(stack_name)
        if stack_info is None:
            results[stack_name] = {
                "status": "error",
                "message": f"Stack '{stack_name}' not found"
            }
            all_success = False
            continue
        
        compose_files = stack_info.get("compose_files", [])
        stack_results = []
        stack_success = True
        
        for compose_path in compose_files:
            try:
                action_func = action_map[action]
                result = action_func(compose_path, timeout=timeout)
                stack_results.append({
                    "compose_file": compose_path,
                    "success": result.returncode == 0,
                    "output": result.stdout,
                    "error": result.stderr
                })
                if result.returncode != 0:
                    stack_success = False
            except Exception as e:
                stack_results.append({
                    "compose_file": compose_path,
                    "success": False,
                    "output": "",
                    "error": str(e)
                })
                stack_success = False
        
        results[stack_name] = {
            "status": "success" if stack_success else "partial_success",
            "action": action,
            "results": stack_results
        }
        
        if not stack_success:
            all_success = False
    
    if all_success:
        log_op("stacks_bulk", f"{action} ({','.join(stack_names)})", "success", f"Applied {action} to {len(stack_names)} stack(s)")
        return jsonify({"status": "success", "action": action, "results": results})
    else:
        log_op("stacks_bulk", f"{action} ({','.join(stack_names)})", "error", "Partial or complete failure")
        return jsonify({"status": "partial_success", "action": action, "results": results}), 207


# ── Notification Settings API ───────────────────────────────────────────────

@app.route("/api/config/notification", methods=["GET"])
def api_get_notification_config():
    """Get current notification settings."""
    from config import NOTIFY_ENABLED, NOTIFY_BACKEND
    try:
        # Merge file settings with environment variable defaults
        file_settings = load_notification_settings()
        
        # Default settings from environment variables
        defaults = {
            "enabled": NOTIFY_ENABLED,
            "backend": NOTIFY_BACKEND,
            "webhook_url": get_env("NOTIFY_WEBHOOK_URL", ""),
            "webhook_method": get_env("NOTIFY_WEBHOOK_METHOD", "POST"),
            "webhook_timeout": get_int_env("NOTIFY_WEBHOOK_TIMEOUT", 10),
            "mqtt_host": get_env("NOTIFY_MQTT_HOST", ""),
            "mqtt_port": get_int_env("NOTIFY_MQTT_PORT", 1883),
            "mqtt_topic": get_env("NOTIFY_MQTT_TOPIC", ""),
            "mqtt_username": get_env("NOTIFY_MQTT_USERNAME", ""),
            "mqtt_password": get_env("NOTIFY_MQTT_PASSWORD", ""),
            "mqtt_retain": get_bool_env("NOTIFY_MQTT_RETAIN", False),
            "email_host": get_env("NOTIFY_EMAIL_HOST", ""),
            "email_port": get_int_env("NOTIFY_EMAIL_PORT", 587),
            "email_username": get_env("NOTIFY_EMAIL_USERNAME", ""),
            "email_password": get_env("NOTIFY_EMAIL_PASSWORD", ""),
            "email_from": get_env("NOTIFY_EMAIL_FROM", ""),
            "email_to": get_env("NOTIFY_EMAIL_TO", ""),
            "email_use_tls": get_bool_env("NOTIFY_EMAIL_USE_TLS", True),
            "on_updates_found": get_bool_env("NOTIFY_ON_UPDATES_FOUND", True),
            "on_pull_success": get_bool_env("NOTIFY_ON_PULL_SUCCESS", False),
            "on_pull_error": get_bool_env("NOTIFY_ON_PULL_ERROR", True),
            "on_recreate_success": get_bool_env("NOTIFY_ON_RECREATE_SUCCESS", False),
            "on_recreate_error": get_bool_env("NOTIFY_ON_RECREATE_ERROR", True),
            "on_bulk_complete": get_bool_env("NOTIFY_ON_BULK_COMPLETE", False),
        }
        
        # File settings override defaults
        final_settings = {**defaults, **file_settings}
        return jsonify(final_settings)
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/config/notification", methods=["POST"])
def api_set_notification_config():
    """Save notification settings."""
    try:
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({"status": "error", "message": "No settings data provided"}), 400
        
        # Save settings to file
        success = save_notification_settings(data)
        if not success:
            return jsonify({"status": "error", "message": "Failed to save settings"}), 500
        
        log_op("config_notification_save", "", "success", "Notification settings saved")
        return jsonify({"status": "success", "message": "Notification settings saved"})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


