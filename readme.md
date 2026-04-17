# 🐳 Docker Update Checker

A self-hosted web dashboard that monitors your Docker Compose stacks for container image updates. It compares local image digests against upstream registry digests and lets you pull updates and recreate services on demand — no automation, full control.

---

## Features

- 🔍 **Recursive compose file scanning** — automatically finds all `docker-compose.yml` / `compose.yml` files under a configurable root directory
- 🔄 **Digest-based update detection** — compares local `RepoDigests` against the upstream registry manifest digest without pulling the image
- 🕐 **Scheduled auto-checks** — configurable interval (default: 60 minutes)
- 🌐 **Multi-registry support** — Docker Hub, GitHub Container Registry (GHCR), Quay.io, and generic OCI-compatible registries
- 🔧 **`.env` file resolution** — resolves `${VAR:-default}` style variables in compose image references
- 📌 **Digest-pinned image handling** — strips `@sha256:` pins and compares by tag
- 🖥️ **Web dashboard** — filterable image table, per-image re-check, pull, and compose recreate buttons
- 📋 **Operations log** — audit trail of every check, pull, and recreate with timestamps
- 🌙 **Dark / light mode** toggle

---

## How Update Detection Works

The app fetches the `Docker-Content-Digest` manifest header directly from the registry API and compares it to the `RepoDigests` value stored with your locally pulled image. If they differ, an update is available. This approach avoids pulling the full image just to check for updates — only the manifest metadata is fetched.

```
Local image RepoDigest  ──┐
                           ├── Match? → Up to date
Registry manifest digest ──┘  No match? → Update available
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

---

## Project Structure

```
docker-update-checker/
├── docker-compose.yml       # Deploys the checker itself
├── .gitignore
├── README.md
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py               # Flask backend + APScheduler
    └── static/
        └── index.html       # Dashboard UI
```

---

## Quick Start

### Prerequisites

- Docker and Docker Compose installed on your host
- Your compose stacks organized under a single root directory (e.g. `/opt/docker`)

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/docker-update-checker.git
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

```
http://localhost:5000
```

---

## Configuration

All configuration is done via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_ROOT` | `/compose` | Path inside the container where your compose files are mounted |
| `CHECK_INTERVAL_MINUTES` | `60` | How often to automatically check for updates |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

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

1. **Check** — the dashboard shows which images have updates available
2. **Pull** — click *Pull Update* to download the new image. Running containers are unaffected at this point
3. **Recreate** — click *↻ Recreate* on the compose project to restart containers using the new image

This two-step process gives you full control over when downtime occurs.

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

---

## License

MIT
"""
