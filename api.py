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
    get_host_resources,
    start_container,
    stop_container,
    restart_container,
)
from notifier import (
    send_notification,
    notify_pull_result,
    notify_recreate_result,
)
from config import NOTIFY_ENABLED, NOTIFY_BACKEND


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
        "version": "1.0.0"
    }), 200


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
            "notify_backend": NOTIFY_BACKEND or None
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
    """List all containers with basic information."""
    all_containers = request.args.get("all", "false").lower() == "true"
    status_filter = request.args.get("status", None)
    
    filters = {}
    if status_filter:
        filters["status"] = status_filter
    
    containers = list_containers(all_containers=all_containers, filters=filters if filters else None)
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


