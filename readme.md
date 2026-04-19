# 🐳 Docker Update Checker

A self-hosted web dashboard that monitors your Docker Compose stacks for container image updates.  
It compares local image digests against upstream registry digests and lets you pull updates and recreate services on demand.  
Right now there is intentionally no automatic updating — this is meant to be super lightweight and manual. If you want heavier, fully automated solutions, tools like Watchtower or Komodo’s global update feature are good alternatives.

---

## Features

- 🔍 **Recursive compose file scanning** — automatically finds all `docker-compose.yml` / `compose.yml` files under a configurable root directory  
- 🔄 **Digest-based update detection** — compares local `RepoDigests` against the upstream registry manifest digest without pulling the image  
- 🕐 **Scheduled auto-checks** — configurable interval (default: 60 minutes)  
- 📦 **Stack grouping** — images are grouped by Compose “stack” (based on directory name) for easier overview and bulk actions  
- 🔄 **Bulk update actions**  
  - **Pull All Updates** — pull all outdated images across all stacks  
  - **Pull All (Selected Stack)** — pull all outdated images in a single stack  
  - Optional **auto-recreate after pull** to restart affected services automatically  
- 🖥️ **Web dashboard**  
  - Filterable and searchable image table  
  - Stack filter and summary  
  - Per-image re-check, pull, and compose recreate buttons  
  - Bulk actions for entire stacks  
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
- 🌙 **Dark / light mode** toggle  
- 🧪 **Test notification** — send a test notification from the UI to validate your configuration  

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

The web UI is available at `http://<your-host>:5000` and refreshes automatically every 30 seconds.

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

---

## Project Structure

```text
docker-update-checker/
├── docker-compose.yml       # Deploys the checker itself
├── .gitignore
├── README.md
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py               # Flask backend + APScheduler + routes
    └── static/
        └── index.html       # Dashboard UI
```

Over time, the backend is modularized into:
- services (compose parsing, image checking, Docker operations, notifications)
- job management and state tracking
- notification dispatchers (webhook, MQTT, email)

---

## Quick Start

### Prerequisites

- Docker and Docker Compose installed on your host  
- Your compose stacks organized under a single root directory (e.g. `/opt/docker`)  

### 1. Clone the repository

```bash
git clone https://github.com/robsterba/docker-update-checker.git
cd docker-update-checker
```

### 2. Configure `docker-compose.yml`

Edit the volume mount to point to your compose root directory:

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

---

## Configuration

All configuration is done via environment variables in `docker-compose.yml`.

### Core Settings

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_ROOT` | `/compose` | Path inside the container where your compose files are mounted |
| `CHECK_INTERVAL_MINUTES` | `60` | How often to automatically check for updates |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `AUTO_RECREATE_AFTER_PULL` | `false` | If `true`, automatically recreate affected services after pulling an image |

### Notification Settings

Notifications are **disabled by default**. Enable them by setting `NOTIFY_ENABLED=true` and choosing a backend.

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_ENABLED` | `false` | Enable notifications (`true` or `false`) |
| `NOTIFY_BACKEND` | *(empty)* | Notification backend: `webhook`, `mqtt`, or `email` |

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

> **Note:** The Docker socket is mounted `:ro` (read-only) by default. If `docker compose up -d` recreate operations fail with a permission error, change it to `:rw` in `docker-compose.yml`.

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