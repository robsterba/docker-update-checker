# 🐳 Docker Homelab Manager

A self-hosted web dashboard for comprehensive Docker homelab management. Originally focused on monitoring container image updates, it now includes container lifecycle management, resource monitoring, and multi-host support.  
Right now there is intentionally no automatic updating — this is meant to be super lightweight and manual. If you want heavier, fully automated solutions, tools like Watchtower or Komodo’s global update feature are good alternatives.

---

## Features

- 🔍 **Recursive compose file scanning** — automatically finds all `docker-compose.yml`, `compose.yml`, `docker-compose.yaml`, or `compose.yaml` files under a configurable root directory  
- 🔄 **Digest-based update detection** — compares local `RepoDigests` against the upstream registry manifest digest without pulling the image  
- 🕐 **Scheduled auto-checks** — configurable interval (default: 60 minutes)  
- 📦 **Stack grouping** — images are grouped by Compose “stack” (based on directory name) for easier overview and bulk actions  
- 🌐 **Remote instance aggregation** — connect to other `docker-update-checker` instances and monitor multiple hosts from one dashboard  
- ✅ **Host selection & health panel** — choose a host, view online/offline status, and inspect last check times across local and remote instances  
- 🧩 **Modular backend architecture** — the backend is split across `app.py`, `api.py`, `config.py`, `docker_utils.py`, `jobs.py`, and `notifier.py` for cleaner separation of concerns and easier remote deployment
- 📊 **Container Monitoring** — view all containers across hosts with real-time status, health checks, and resource information
- ▶️ **Container Lifecycle Management** — start, stop, restart containers directly from the dashboard
- 🏥 **Health Check Visualization** — color-coded health status badges for containers with health checks configured  
- 🔄 **Bulk update actions**  
  - **Pull All Updates** — pull all outdated images across all stacks  
  - **Pull All (Selected Stack)** — pull all outdated images in a single stack  
  - Optional **auto-recreate after pull** to restart affected services automatically  
- 🖥️ **Web dashboard**  
  - Filterable and searchable image table  
  - Stack filter and summary  
  - Per-image re-check, pull, and compose recreate buttons  
  - Bulk actions for entire stacks  
  - Live host status badge and remote host selector  
- 📋 **Operations log** — audit trail of every check, pull, recreate, and bulk job  
- 🔔 **Notifications** — optional event-driven notifications via:
  - Webhook (e.g. Home Assistant)
  - MQTT
  - Email  
  Notifications can be sent for:
  - New updates found during checks
  - Pull success / failure
  - Recreate success / failure
  - Bulk job completion  
- 🌙 **Dark / light mode** toggle — preserves your last selected theme across browser reloads
- 💾 **Persistent UI preferences** — auto-recreate and theme selections are remembered in the dashboard
- 🧪 **Test notification** — send a test notification from the UI to validate your configuration  
- 📋 **Container Management API** — REST endpoints for programmatic container control
- 📝 **Compose File Management** — view, edit, and validate compose files from the dashboard
- ▶️ **Stack Lifecycle Management** — start, stop, restart entire stacks with one click
- 🔗 **Dependency Visualization** — view service relationships and network connections in compose files
- ✅ **Bulk Stack Operations** — apply actions across multiple stacks simultaneously

---

## Container Management API

The following endpoints are available for container management:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/containers` | List all containers (supports `?all=true` and `?status=running` filters) |
| GET | `/api/containers/{id}` | Get detailed container information |
| GET | `/api/containers/{id}/resources` | Get CPU/memory resource usage |
| GET | `/api/host/resources` | Get Docker host resource summary |
| POST | `/api/containers/{id}/start` | Start a stopped container |
| POST | `/api/containers/{id}/stop` | Stop a running container (supports `?timeout=10` parameter) |
| POST | `/api/containers/{id}/restart` | Restart a container (supports `?timeout=10` parameter) |

### System API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/version` | Get the application version |

### Compose File Management API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/compose/files/detailed` | List all compose files with metadata (services, images, project) |
| GET | `/api/compose/files/{path}` | Get compose file content as parsed YAML |
| PUT | `/api/compose/files/{path}` | Update compose file content (creates backup automatically) |
| POST | `/api/compose/files/{path}/validate` | Validate compose file YAML syntax |
| GET | `/api/compose/files/{path}/dependencies` | Get dependency graph (services, networks, volumes) |

### Stack Management API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stacks/all` | Get all stacks with services, containers, and status |
| GET | `/api/stacks/{name}` | Get specific stack information |
| GET | `/api/stacks/{name}/containers` | List containers belonging to a stack |
| GET | `/api/stacks/{name}/status` | Get overall stack status (running/stopped/mixed) |
| POST | `/api/stacks/{name}/up` | Start stack (`docker compose up -d`) |
| POST | `/api/stacks/{name}/down` | Stop stack (`docker compose down`) |
| POST | `/api/stacks/{name}/restart` | Restart all containers in stack |
| POST | `/api/stacks/bulk` | Apply action (up/down/restart) to multiple stacks |

---

## How Update Detection Works

The app fetches the `Docker-Content-Digest` manifest header directly from the registry API and compares it to the `RepoDigests` value stored with your locally pulled image. If they differ, an update is available. This approach avoids pulling the full image just to check for updates — only the manifest metadata is fetched.

```text
Local image RepoDigest  ——┐
                          ├── Match? → Up to date
Registry manifest digest ——╝  No match? → Update available
```

Images defined with `build:` instead of `image:` in a compose file are ignored, since they have no upstream registry to compare against.

---

## Dashboard

The web UI is available at `http://<your-host>:5000` and refreshes automatically every 10 seconds.

### Status Badges

| Badge | Meaning |
|---|---|
| ✓ Up to Date | Local digest matches registry |
| ↑ Update Available | Registry has a newer manifest |
| ✗ Registry Error | Could not reach the registry |
| ? Not Pulled | Image is referenced but not pulled locally |
| ? Unknown | Could not determine status |

### Actions per Image

- **Re-check** — fetches the latest digest for that image only  
- **Pull Update** — runs `docker pull` to download the new image layers (containers keep running on the old image)  
- **↻ Recreate** — runs `docker compose up -d --remove-orphans` on the associated compose project to restart containers on the new image  

### Bulk Actions

- **Pull All Updates** — pulls all images with `update_available` or `not_pulled` status across all stacks  
- **Pull All (Stack)** — pulls all outdated images in a selected stack  
- **↻ Recreate Stack** — recreates all services in a selected stack  
- Optional **auto-recreate after pull** — when enabled, affected services/stacks are automatically recreated after pulling new images  

### Stack Management

Each stack card now displays:
- **Service count** — number of services defined in compose files
- **Container count** — number of running containers in the stack
- **Status badge** — overall stack state (Running / Stopped / Mixed)

Stack actions available:
- **Details** — view compose files, services, containers, and dependency graph
- **Start** — start all containers in the stack (`docker compose up -d`)
- **Restart** — restart all containers in the stack
- **Stop** — stop and remove all containers in the stack (`docker compose down`)
- Actions are disabled based on current stack state to prevent errors

### Compose File Editor

Accessible from the **Compose Files** dropdown menu:
- **View All Compose Files** — grid view of all discovered compose files with service and image counts
- **New Compose File** — create a new compose file from scratch

Editor features:
- Full YAML editing with syntax highlighting
- **Validate** — check YAML syntax and structure
- **Dependencies** — view service dependency graph, networks, and volumes
- **Save** — writes file with automatic backup (creates `.bak` file)
- Auto-formatting and error feedback

---

## Project Structure

```text
docker-update-checker/
├── app.py
├── api.py                   # Flask API route handlers
├── config.py                # environment parsing and configuration
├── docker_utils.py          # Docker and compose helpers
├── jobs.py                  # job state, progress tracking, and operation log
├── notifier.py              # notification backends (webhook, MQTT, email)
├── schemas.py               # Pydantic schemas for request validation
├── compose.example.yaml     # Compose file template for deploying the checker
├── Dockerfile
├── requirements.txt
├── readme.md
└── static/
    └── index.html           # Dashboard UI
```

The backend is now modularized into:
- `config.py` for runtime configuration and environment variables
- `docker_utils.py` for compose scanning, image checks, Docker compose operations, and Phase 2 compose file/stack management
- `jobs.py` for background job tracking and operation logging
- `notifier.py` for webhook, MQTT, and email notification delivery
- `schemas.py` for Pydantic request validation schemas
- `api.py` for the Flask route handlers and HTTP API surface

`app.py` boots the Flask app, initializes the scheduler, and imports `api` to register HTTP routes.

---

## Quick Start

### Prerequisites

- Docker and Docker Compose installed on your host  
- Your compose stacks organized under a single root directory (e.g. `/opt/docker`)  

### 1. Clone the repository

```bash
git clone https://github.com/your-username/docker-update-checker.git
cd docker-update-checker
```

### 2. Copy and configure `compose.yaml`

Copy the template file and edit the volume mount to point to your compose root directory:

```bash
cp compose.example.yaml compose.yaml
```

Then edit `compose.yaml`:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
  - /opt/docker:/compose:ro   # ← change /opt/docker to your path
```

### 3. Build and start

```bash
docker compose up -d --build
```

### 4. Open the dashboard

```text
http://localhost:5000
```

If you deploy this service to multiple nodes, make sure each node is built and restarted with the latest source changes. Remote instances are aggregated via `REMOTE_INSTANCES` or `REMOTE_INSTANCES_FILE`, and each node must expose its local API on port `5000`.

---

## Configuration

All configuration is done via environment variables in your `compose.yaml` (copied from `compose.example.yaml`), or by copying `.env.example` to `.env` and customizing values.

### Core Settings

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_ROOT` | `/compose` | Path inside the container where your compose files are mounted |
| `CHECK_INTERVAL_MINUTES` | `60` | How often to automatically check for updates |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `AUTO_RECREATE_AFTER_PULL` | `false` | If `true`, automatically recreate affected services after pulling an image |

### Remote Instance Configuration

The dashboard can aggregate status from other `docker-update-checker` instances running on remote hosts.

- Use `REMOTE_INSTANCES` to provide a JSON array of remote hosts.
- Or use `REMOTE_INSTANCES_FILE` to point to a JSON file such as `remote_instances.example.json`.

Example config entry:
```json
{
  "name": "Homelab Node 1",
  "url": "http://192.168.1.10:5000",
  "description": "Checker instance on remote node"
}
```

You can also use a simple newline-based list in `REMOTE_INSTANCES` like:
```text
node1|http://192.168.1.10:5000
node2|http://192.168.1.11:5000
```

### Notification Settings

Notifications are **disabled by default**. Enable them by setting `NOTIFY_ENABLED=true` and choosing a backend.

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_ENABLED` | `false` | Enable notifications (`true` or `false`) |
| `NOTIFY_BACKEND` | *(empty)* | Notification backend: `webhook`, `mqtt`, or `email` |
| `NOTIFY_MAX_FREQUENCY` | `0` | Throttle notifications (seconds between sends, `0` = disabled) |

#### Webhook Notifications

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_WEBHOOK_URL` | *(empty)* | Webhook URL (e.g. Home Assistant: `http://homeassistant.local:8123/api/webhook/<webhook_id>`) |
| `NOTIFY_WEBHOOK_METHOD` | `POST` | HTTP method: `POST` or `PUT` |
| `NOTIFY_WEBHOOK_TIMEOUT` | `10` | Request timeout in seconds |

#### MQTT Notifications

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_MQTT_HOST` | *(empty)* | MQTT broker host |
| `NOTIFY_MQTT_PORT` | `1883` | MQTT broker port |
| `NOTIFY_MQTT_TOPIC` | *(empty)* | Topic to publish notifications to |
| `NOTIFY_MQTT_USERNAME` | *(empty)* | MQTT username (optional) |
| `NOTIFY_MQTT_PASSWORD` | *(empty)* | MQTT password (optional) |
| `NOTIFY_MQTT_RETAIN` | `false` | Whether to retain MQTT messages |

#### Email Notifications

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_EMAIL_HOST` | *(empty)* | SMTP server host |
| `NOTIFY_EMAIL_PORT` | `587` | SMTP server port |
| `NOTIFY_EMAIL_USERNAME` | *(empty)* | SMTP username |
| `NOTIFY_EMAIL_PASSWORD` | *(empty)* | SMTP password |
| `NOTIFY_EMAIL_FROM` | *(empty)* | Sender email address |
| `NOTIFY_EMAIL_TO` | *(empty)* | Recipient email address |
| `NOTIFY_EMAIL_USE_TLS` | `true` | Use STARTTLS (`true` or `false`) |

### Notification Triggers

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_ON_UPDATES_FOUND` | `true` | Notify when new updates are detected during a check |
| `NOTIFY_ON_PULL_SUCCESS` | `false` | Notify on successful image pull |
| `NOTIFY_ON_PULL_ERROR` | `true` | Notify on pull failure |
| `NOTIFY_ON_RECREATE_SUCCESS` | `false` | Notify on successful compose recreate |
| `NOTIFY_ON_RECREATE_ERROR` | `true` | Notify on recreate failure |
| `NOTIFY_ON_BULK_COMPLETE` | `true` | Notify when a bulk pull/recreate job completes |

---

## Volume Mounts

| Host Path | Container Path | Purpose |
|---|---|---|
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker API access for image pulls and compose operations |
| `/opt/docker` *(your path)* | `/compose` | Directory scanned recursively for compose files |

> **⚠️ Docker Socket Permissions - Security Consideration**
> 
> The Docker socket is mounted **read-only (`:ro`)** by default in the `compose.example.yaml` template. This is more secure but has limitations:
> 
> | Mount Mode | Can Pull Images | Can Run `docker compose up -d` | Security |
> |---|---|---|---|
> | `:ro` (read-only) | ✅ Yes | ❌ No - will fail with permission error | **More secure** |
> | `:rw` (read-write) | ✅ Yes | ✅ Yes | Less secure |
> 
> **Recommendation:** 
> - Use `:ro` if you only need to **check for updates** and **pull images** (the app can still pull images via the Docker SDK with read-only socket)
> - Use `:rw` if you need **automatic service recreation** (`docker compose up -d`)
> - For maximum security: Keep as `:ro` and manually recreate containers after pulling updates via the dashboard

---

## Supported Registries

| Registry | Authentication Method |
|---|---|
| Docker Hub (`docker.io`) | Anonymous token via `auth.docker.io` |
| GitHub Container Registry (`ghcr.io`) | Anonymous token via `ghcr.io/token` |
| Quay.io and others | Unauthenticated manifest HEAD request |
| Private registries | Unauthenticated fallback (add auth support as needed) |

For private registries that require credentials, pre-authenticate on the host with `docker login <registry>` and mount your Docker config into the container:

```yaml
volumes:
  - /root/.docker/config.json:/root/.docker/config.json:ro
```

---

## Updating Your Stacks (Recommended Workflow)

Update detection and applying updates are intentionally separated — nothing updates automatically without your approval.

### Standard Workflow

1. **Check** — the dashboard shows which images have updates available  
2. **Pull** — click *Pull Update* to download the new image. Running containers are unaffected at this point  
3. **Recreate** — click *↻ Recreate* on the compose project to restart containers using the new image  

This two-step process gives you full control over when downtime occurs.

### Bulk Workflow with Notifications

1. **Check** — run a full check or wait for the scheduled interval  
2. **Bulk Pull**  
   - Click **Pull All Updates** for all stacks, or  
   - Select a stack and pull all updates for that stack only  
3. **Optional Auto-Recreate**  
   - If `AUTO_RECREATE_AFTER_PULL=true` or you enable it per-job, affected services are recreated automatically after pulling  
4. **Notifications**  
   - You can configure webhooks (e.g. Home Assistant), MQTT, or email to receive:
     - Alerts when updates are found
     - Pull success/failure
     - Recreate success/failure
     - Bulk job completion summaries  

---

## Updating the Checker Itself

```bash
cd docker-update-checker
git pull
docker compose down
docker compose up -d --build
```

---

## Troubleshooting

**Dashboard shows "Registry Error" for all images**  
The container cannot reach the registry. Add DNS servers to `docker-compose.yml`:

```yaml
dns:
  - 8.8.8.8
  - 1.1.1.1
```

**Images with `${VARIABLE}` in their name show as unknown**  
The app attempts to resolve variables from a `.env` file in the same directory as the compose file. Make sure your `.env` file exists and contains the variable definition.

**Digest-pinned images (`image@sha256:...`) not resolving**  
The app automatically strips the `@sha256:` digest and compares by tag. If you see errors, check that the tag portion of the image reference is valid.

**`docker compose up -d` recreate fails with permission error**  
Change the Docker socket mount from `:ro` to `:rw` in `docker-compose.yml`.

**Container logs show `401 Unauthorized` for Docker Hub official images**  
Official images (e.g. `redis`, `nginx`) require the `library/` namespace prefix. This is handled automatically — if you still see 401s, ensure the image name in your compose file does not include an explicit `docker.io/` prefix combined with no namespace (e.g. use `redis:latest` not `docker.io/redis:latest`).

**Notifications not being received**  
- Confirm `NOTIFY_ENABLED=true`  
- Confirm `NOTIFY_BACKEND` is set to `webhook`, `mqtt`, or `email`  
- Check that the required variables for your chosen backend are set  
- Use the **Test Notification** button in the UI to verify delivery  
- For Home Assistant, ensure the webhook is configured with `local_only: true` and that your `NOTIFY_WEBHOOK_URL` matches the `webhook_id`

---

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web framework and REST API |
| `flask-cors` | Cross-origin request handling |
| `docker` | Python Docker SDK for image pulls and local digest lookup |
| `requests` | Registry API HTTP calls |
| `pyyaml` | Compose file parsing |
| `apscheduler` | Background scheduled checks |
| `paho-mqtt` *(optional)* | MQTT notification support |

---

## License

MIT