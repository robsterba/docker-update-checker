from flask import send_from_directory, jsonify, request, Response
import app
from app import *


# ── Routes (moved from app.py) ─────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    with app.state_lock:
        return jsonify({
            "last_check": app.last_full_check,
            "total": len(app.check_results),
            "up_to_date": sum(1 for r in app.check_results.values()
                              if r["status"] == "up_to_date"),
            "updates_available": sum(1 for r in app.check_results.values()
                                     if r["status"] == "update_available"),
            "unknown": sum(1 for r in app.check_results.values()
                           if r["status"] in ("unknown", "registry_error", "not_pulled")),
            "check_interval_minutes": app.CHECK_INTERVAL_MINUTES,
            "auto_recreate_after_pull": app.AUTO_RECREATE_AFTER_PULL,
            "notify_enabled": app.NOTIFY_ENABLED,
            "notify_backend": app.NOTIFY_BACKEND or None
        })


@app.route("/api/instances")
def api_instances():
    return jsonify(get_all_instances())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global AUTO_RECREATE_AFTER_PULL

    if request.method == "GET":
        return jsonify({"auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL})

    data = request.get_json(silent=True) or {}
    if "auto_recreate" not in data:
        return jsonify({"status": "error", "message": "Missing auto_recreate"}), 400

    auto_recreate = data["auto_recreate"]
    if isinstance(auto_recreate, str):
        auto_recreate = auto_recreate.strip().lower() == "true"
    elif not isinstance(auto_recreate, bool):
        return jsonify({"status": "error", "message": "auto_recreate must be true or false"}), 400

    AUTO_RECREATE_AFTER_PULL = auto_recreate
    log_op("config", "auto_recreate", "success", f"Set auto_recreate_after_pull={AUTO_RECREATE_AFTER_PULL}")
    return jsonify({"auto_recreate_after_pull": AUTO_RECREATE_AFTER_PULL})


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


