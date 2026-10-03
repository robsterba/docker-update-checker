# Docker Update Checker

A lightweight, self-hosted web dashboard for monitoring and managing Docker container image updates in a homelab. Update detection and applying updates are intentionally separate: nothing is ever updated automatically without your approval. If you want fully automated updates, tools like Watchtower or Komodo are good alternatives.

The current version lives in the [`VERSION`](VERSION) file; release history is in the [`CHANGELOG.md`](CHANGELOG.md).

## Features

- **Recursive compose scanning** — finds all `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, and `compose.yaml` files under a configurable root directory
- **Digest-based update detection** — compares local `RepoDigests` against the upstream registry manifest digest, without pulling the image
- **Stack grouping** — images are grouped by compose stack (directory name) with per-stack summaries and actions
- **Pull & recreate workflow** — pull updates per image, per stack, or in bulk; optionally auto-recreate affected services after pulling
- **Container lifecycle management** — start, stop, and restart any container; view health status and CPU/memory usage
- **Stack lifecycle management** — bring stacks up, down, or restart them; bulk actions across multiple stacks
- **Compose file editor** — view, edit, validate, and create compose files from the dashboard, with automatic backups
- **Host monitoring** — CPU, memory, and disk usage for the Docker host, plus per-container resource stats
- **OS package update monitoring** — tracks available host package updates via a host-level agent (see [`scripts/README.md`](scripts/README.md))
- **Self-update notifications** — checks GitHub for newer releases of this app and can notify you
- **Remote instance aggregation** — monitor other docker-update-checker instances from a single dashboard
- **Notifications** — webhook (e.g. Home Assistant), MQTT, or email; optional batching into summary notifications
- **Scheduled auto-checks** — configurable interval (default: 60 minutes)
- **Themes** — light, dusk-blue dark, and AMOLED black, with OS-preference detection and remembered preference

## How Update Detection Works

The app fetches the `Docker-Content-Digest` manifest header from the registry API and compares it with the `RepoDigests` value stored with the locally pulled image:

```text
Local image RepoDigest  ──┐
                          ├── Match? → Up to date
Registry manifest digest ─╝  No match? → Update available
```

Only manifest metadata is fetched — the full image is never pulled just to check for updates.

Image statuses:

| Status | Meaning |
|---|---|
| Up to Date | Local digest matches registry |
| Update Available | Registry has a newer manifest |
| Registry Error | Could not reach the registry |
| Not Pulled | Image is referenced by a compose file but not pulled locally |
| Unknown | Could not determine status |

Services defined with `build:` instead of `image:` are ignored (no upstream registry). `${VARIABLE}` references in image names are resolved from a `.env` file next to the compose file; unresolvable references are skipped.

## Quick Start

Requires Docker and Docker Compose, with your compose stacks organized under a single root directory.

```bash
git clone https://github.com/robsterba/docker-update-checker.git
cd docker-update-checker
cp compose.example.yaml compose.yaml
# edit compose.yaml: point the /compose volume mount at your compose root
docker compose up -d --build
```

The dashboard is available at `http://<host>:5000` and refreshes automatically every 10 seconds (configurable in the UI, minimum 5 seconds).

To update the checker itself:

```bash
git pull
docker compose up -d --build
```

## Configuration

All configuration is via environment variables in the `environment:` block of your `compose.yaml`. Defaults:

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_ROOT` | `/compose` | Directory (inside the container) scanned for compose files |
| `CHECK_INTERVAL_MINUTES` | `60` | Automatic check interval |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `AUTO_RECREATE_AFTER_PULL` | `false` | Recreate affected services automatically after pulling |
| `TOKEN_CACHE_TTL` | `900` | Registry token cache TTL (seconds) |
| `REGISTRY_DELAY_SECONDS` | `0` | Delay between registry API calls (rate limiting) |
| `APP_SETTINGS_FILE` | `app_settings.json` | Runtime-persisted settings (check interval, etc.) |
| `NOTIFICATION_SETTINGS_FILE` | `notification_settings.json` | Runtime-persisted notification settings |

### Remote Instances

Aggregate other docker-update-checker instances on remote hosts. Set `REMOTE_INSTANCES` to a JSON array, or `REMOTE_INSTANCES_FILE` to a path (mounted into the container) containing one:

```json
[
  {"name": "Node 1", "url": "http://192.168.1.10:5000", "description": "attic server"}
]
```

`REMOTE_INSTANCES` also accepts a newline-delimited `name|url` list. Remote instances are reachable through the dashboard's host selector; requests are proxied through the local instance.

### Self-Update Checks

| Variable | Default | Description |
|---|---|---|
| `SELF_UPDATE_CHECK_ENABLED` | `true` | Check GitHub for newer releases |
| `SELF_UPDATE_CHECK_INTERVAL_HOURS` | `24` | Check interval |

### OS Package Updates

| Variable | Default | Description |
|---|---|---|
| `OS_UPDATE_CHECK_ENABLED` | `true` | Enable OS package update checks |
| `OS_UPDATE_CHECK_INTERVAL_HOURS` | `24` | Check interval |

For production hosts, deploy the host-level agent described in [`scripts/README.md`](scripts/README.md) and mount its output file:

```yaml
volumes:
  - /var/lib/docker-update-checker/os-updates.json:/var/lib/docker-update-checker/os-updates.json:ro
```

### Notifications

Disabled by default. Enable with `NOTIFY_ENABLED=true` and set `NOTIFY_BACKEND` to `webhook`, `mqtt`, or `email`, plus the backend-specific variables (`NOTIFY_WEBHOOK_URL`, `NOTIFY_MQTT_HOST`/`NOTIFY_MQTT_TOPIC`, `NOTIFY_EMAIL_HOST`/`NOTIFY_EMAIL_FROM`/`NOTIFY_EMAIL_TO`, etc. — see [`.env.example`](.env.example) for the full list).

Trigger controls (all optional):

| Variable | Default | Description |
|---|---|---|
| `NOTIFY_ON_UPDATES_FOUND` | `true` | New updates detected during a check |
| `NOTIFY_ON_PULL_SUCCESS` | `false` | Image pull succeeded |
| `NOTIFY_ON_PULL_ERROR` | `true` | Image pull failed |
| `NOTIFY_ON_RECREATE_SUCCESS` | `false` | Compose recreate succeeded |
| `NOTIFY_ON_RECREATE_ERROR` | `true` | Compose recreate failed |
| `NOTIFY_ON_BULK_COMPLETE` | `false` | Bulk job completed |
| `NOTIFY_MAX_FREQUENCY` | `0` | Throttle: minimum seconds between notifications (0 = off) |
| `NOTIFY_SUMMARY_ENABLED` | `true` | Batch pull/recreate/bulk notifications into summaries |
| `NOTIFY_BATCH_WINDOW` | `300` | Summary batch window (seconds) |

Use the **Test Notification** action in the dashboard to verify your configuration.

### About `.env`

The container does not read a `.env` file directly. If you copy `.env.example` to `.env`, Docker Compose only uses it for variable *interpolation* in `compose.yaml` (e.g. `${COMPOSE_ROOT}`). Application settings must be listed in the `environment:` block of `compose.yaml` to reach the container.

## Dashboard

- **Image table** — filterable and searchable, with per-image re-check, pull, and recreate actions
- **Stack cards** — service/container counts, status badges (Running / Stopped / Mixed), and per-stack pull/recreate actions
- **Containers view** — all containers across hosts with status, health badges, and resource usage
- **Bulk actions** — pull all updates (all stacks or a selected stack), with optional auto-recreate
- **Jobs panel** — live progress for every check, pull, recreate, and prune operation
- **Operations log** — audit trail of all actions

The recommended update workflow is deliberately two-step:

1. **Pull** — download the new image (running containers keep using the old one)
2. **Recreate** — `docker compose up -d` on the affected stack to switch containers to the new image

## Security

**This application has no authentication.** Anyone who can reach port 5000 can inspect and control your Docker host: pull images, start/stop containers and stacks, edit compose files, and prune resources.

- Do not expose the dashboard to the public internet. Bind it to your LAN or put it behind an authenticating reverse proxy or VPN.
- The Docker socket mount grants full API access regardless of whether it is mounted `:ro` — the read-only flag applies to the socket file, not to the API operations performed through it. Treat the socket as equivalent to root access and keep the container on a trusted network.
- `compose.yaml`, `remote_instances.json`, and `notification_settings.json` may contain host-specific details and are gitignored.

## API

All endpoints are under `/api`. Highlights:

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check (Docker connectivity, version) |
| GET | `/api/version` | Application version |
| GET | `/api/status` | Check state summary |
| GET | `/api/images` | Image check results |
| GET | `/api/stacks/all` | All stacks with services, containers, and status |
| POST | `/api/check` | Start a full update check |
| POST | `/api/check/<image_ref>` | Re-check a single image |
| POST | `/api/update/<image_ref>` | Pull an image (optional `auto_recreate`) |
| POST | `/api/bulk/update` | Bulk pull (`stack`, `auto_recreate`) |
| POST | `/api/compose/recreate` | Recreate a stack from a compose file |
| GET/POST | `/api/containers` | List containers (filters: `all`, `status`, `resources`) |
| POST | `/api/containers/<id>/start` / `stop` / `restart` | Container lifecycle |
| GET | `/api/containers/<id>/resources` | Per-container CPU/memory usage |
| GET | `/api/host/resources` | Host CPU, memory, and disk usage |
| GET | `/api/host/os-updates` | OS package update information |
| GET/PUT | `/api/compose/files/<path>` | Read or write a compose file (writes create a `.bak`) |
| POST | `/api/compose/files/<path>/validate` | Validate compose YAML |
| POST | `/api/prune/containers` / `images` / `system` / `volumes` | Prune operations |
| POST | `/api/stacks/<name>/up` / `down` / `restart` | Stack lifecycle |
| POST | `/api/stacks/bulk` | Apply an action to multiple stacks |
| GET/POST | `/api/config` | Read or update auto-recreate and check interval |
| GET/POST | `/api/config/notification` | Read or update notification settings |
| GET/POST | `/api/instances/remote` | Read or update remote instance configuration |
| GET | `/api/instances` | Local + remote instances |
| ANY | `/api/instances/<id>/<path>` | Proxy an API call to a local or remote instance |
| GET | `/api/jobs` / `/api/jobs/<id>` | Job list and details |
| GET | `/api/operations` | Operations log |
| POST | `/api/notify/test` | Send a test notification |
| GET | `/api/checker/updates` | Check for a newer release of this app |

The same endpoints power the dashboard; the instance proxy allows a remote dashboard to operate a remote host's API.

## Project Structure

```text
docker-update-checker/
├── app.py                   # entry point: route registration, startup validation, scheduler
├── services.py               # Flask app object, orchestration, proxying, jobs
├── api.py                    # HTTP route handlers
├── config.py                 # environment parsing and configuration
├── docker_utils.py           # Docker/compose helpers, image checks, registry API
├── jobs.py                   # job state, progress tracking, operation log
├── notifier.py               # notification backends (webhook, MQTT, email)
├── schemas.py                # Pydantic request validation schemas
├── version.py                # reads the VERSION file
├── static/index.html         # dashboard UI (single file, no build step)
├── scripts/                  # host-level OS update agent (see scripts/README.md)
├── compose.example.yaml      # deployment template
├── .env.example              # documented environment variables
├── style.md                  # web theme spec used by the dashboard
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
└── tests/                    # pytest suite
```

Import flow: `app.py` → `services.py` (business logic, owns the Flask app) and `api.py` (routes) → `config.py`, `docker_utils.py`, `jobs.py`, `notifier.py`, `schemas.py`. There are no circular imports; importing any module has no side effects until `app.py` runs as the entry point.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # or .venv/bin/pip on Linux/macOS

# run the test suite (no Docker daemon or network access required)
python -m pytest

# lint
python -m ruff check .
```

The test suite covers image reference parsing, compose file discovery and validation, the registry digest flow (with HTTP mocked), job tracking, notification gating and batching, remote instance handling, the proxy layer, and the API surface via Flask's test client.

## Supported Registries

| Registry | Authentication |
|---|---|
| Docker Hub (`docker.io`) | Anonymous token via `auth.docker.io` |
| GitHub Container Registry (`ghcr.io`) | Anonymous token via `ghcr.io/token` |
| Quay.io and others | Unauthenticated manifest HEAD request |
| Private registries | Unauthenticated fallback |

For private registries, pre-authenticate on the host with `docker login <registry>` and mount your Docker config into the container:

```yaml
volumes:
  - /root/.docker/config.json:/root/.docker/config.json:ro
```

## Troubleshooting

**Dashboard shows "Registry Error" for all images** — the container cannot reach the registry. Add DNS servers to your compose file:

```yaml
dns:
  - 8.8.8.8
  - 1.1.1.1
```

**Images with `${VARIABLE}` in their name show as unknown** — the app resolves variables from a `.env` file in the same directory as the compose file. Ensure the file exists and defines the variable.

**Digest-pinned images (`image@sha256:...`) not resolving** — the digest is stripped and the image compared by tag. Ensure the tag portion is valid.

**401 Unauthorized for Docker Hub official images** — official images are prefixed with `library/` automatically. Avoid combining an explicit `docker.io/` prefix with no namespace (use `redis:latest`, not `docker.io/redis:latest`).

**Notifications not received** — confirm `NOTIFY_ENABLED=true`, `NOTIFY_BACKEND` is set, the backend-specific variables are configured, and use the **Test Notification** action. For Home Assistant, ensure the webhook `webhook_id` matches `NOTIFY_WEBHOOK_URL`.

## License

MIT
