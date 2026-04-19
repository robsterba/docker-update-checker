# Changelog

All notable changes to **Docker Update Checker** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to a manual-release model (no automated version numbering yet).

---

## [Unreleased]

### Added

- **Phase 3 — Notification framework**
  - Generic notification dispatcher supporting:
    - Webhook (e.g. Home Assistant)
    - MQTT
    - Email  
  - Events notified:
    - New updates found during checks (`updates_found`)
    - Pull success / failure (`pull_result`)
    - Recreate success / failure (`recreate_result`)
    - Bulk job completion (`bulk_complete`)
    - Test notification (`test`)
  - Environment-driven configuration:
    - `NOTIFY_ENABLED`, `NOTIFY_BACKEND`
    - Webhook: `NOTIFY_WEBHOOK_URL`, `NOTIFY_WEBHOOK_METHOD`, `NOTIFY_WEBHOOK_TIMEOUT`
    - MQTT: `NOTIFY_MQTT_HOST`, `NOTIFY_MQTT_PORT`, `NOTIFY_MQTT_TOPIC`, user/pass, retain
    - Email: SMTP host/port, user/pass, from/to, TLS
  - Per-event notification toggles:
    - `NOTIFY_ON_UPDATES_FOUND`
    - `NOTIFY_ON_PULL_SUCCESS`
    - `NOTIFY_ON_PULL_ERROR`
    - `NOTIFY_ON_RECREATE_SUCCESS`
    - `NOTIFY_ON_RECREATE_ERROR`
    - `NOTIFY_ON_BULK_COMPLETE`
  - `/api/notify/test` endpoint and UI **Test Notification** button

- **Bulk update actions**
  - **Pull All Updates** — pull all outdated images across all stacks
  - **Pull All (Selected Stack)** — pull all outdated images in a single stack
  - Bulk jobs are tracked as background jobs with progress

- **Auto-recreate after pull**
  - `AUTO_RECREATE_AFTER_PULL` environment variable
  - Optional auto-recreate of affected services/stacks after pulling new images
  - Works for:
    - Single-image pulls
    - Bulk pulls (by stack or all stacks)

- **Stack grouping and stack-level actions**
  - compose “stacks” inferred from directory names under `COMPOSE_ROOT`
  - Images grouped by stack in the dashboard
  - Stack filter in the UI
  - Stack summary (total images, updates available, up-to-date count)
  - **Recreate Stack** action — recreate all services in a selected stack

- **Job tracking and progress**
  - Background jobs tracked with:
    - `job_id`
    - status (`pending`, `running`, `success`, `error`)
    - progress / total steps
    - current step description
    - optional event stream per step
  - `/api/jobs`, `/api/jobs/<job_id>`, `/api/jobs/<job_id>/stop` endpoints
  - UI shows live job list and progress

- **Improved Docker Compose integration**
  - Per-service recreate using `docker compose up -d --no-deps <service>` when possible
  - Stack-wide recreate as fallback

- **Dashboard improvements**
  - Stack filter dropdown
  - Stack summary cards
  - Bulk action buttons
  - Job list with progress bars
  - Test Notification button

### Changed

- Update detection and applying updates remain intentionally manual; no automatic image pulls or container restarts without user confirmation.
- Notification system is **off by default**; must be enabled explicitly with `NOTIFY_ENABLED=true`.
- Default behavior for `AUTO_RECREATE_AFTER_PULL` is `false`.

### Documentation

- Updated `README.md` to document:
  - Notification configuration and backends
  - Bulk actions and auto-recreate
  - Stack grouping and stack-level operations
  - New environment variables
- Added this `CHANGELOG.md`

---

## [v0.1.0] – Initial Release

### Added

- Self-hosted web dashboard for Docker Compose image update monitoring
- Recursive compose file scanning under `COMPOSE_ROOT`
- Digest-based update detection via registry manifest digests
- Scheduled auto-checks with configurable interval (`CHECK_INTERVAL_MINUTES`)
- Multi-registry support:
  - Docker Hub (`docker.io`)
  - GitHub Container Registry (`ghcr.io`)
  - Quay.io and generic OCI-compatible registries
- `.env` file resolution for `${VAR:-default}` and `${VAR}` patterns in image references
- Digest-pinned image handling (`@sha256:...` stripped, compare by tag)
- Per-image actions:
  - Re-check
  - Pull Update
  - Recreate (via `docker compose up -d --remove-orphans`)
- Operations log with timestamps for checks, pulls, and recreates
- Dark / light mode toggle in the UI
- Basic responsive layout with filterable image table

### Changed

- Intentionally no automatic updates; user must explicitly pull and recreate.

### Documentation

- Initial `README.md` with:
  - Feature list
  - How update detection works
  - Dashboard usage
  - Quick start
  - Configuration via environment variables
  - Volume mounts
  - Supported registries
  - Recommended workflow
  - Troubleshooting