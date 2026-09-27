# Changelog

All notable changes to **Docker Update Checker** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to a manual-release model (no automated version numbering yet).

---

## [v1.2.0] — 2026-09-26

### Changed

- **Standard web theme (style.md) applied across the dashboard**
  - Light and dark palettes remapped to the brand palette: deep plum-charcoal (`#2A2539`), slate charcoal (`#353044`), muted violet-gray (`#5E5373`), pale lavender (`#DBD8E3`)
  - Dark theme uses the inverted accent (light fill, dark text) per the spec
  - Status colors (success/warning/error/info) use the spec's muted light and brightened dark variants
  - Badge, notification, and validation tints derived from the new status colors — no hard-coded hex values remain outside the token definitions
  - Modals, compose editor, and code blocks use the inset surface token; inputs use the spec's stronger border

### Added

- Focus-visible ring per the spec, and a 0.25s background/color transition when switching themes

---

## [v1.1.2] — 2026-09-26

### Changed

- Dashboard loading no longer blocks on the slowest host: cards render immediately with a "Checking…" state and update individually as each host's status arrives
- Startup data loads (status, images, stacks, containers, update check, OS updates) run in parallel instead of serially
- Remote instance proxy uses a 3-second connect timeout so unreachable hosts fail fast instead of stalling for 15 seconds

### Fixed

- Dashboard could sit on "Loading host status…" with empty KPI fields when any remote host was slow or unreachable

---

## [v1.1.1] — 2026-09-26

### Added

- **Tabbed dashboard UI**
  - Sections reorganized into tabs: Overview, Images, Containers, Stacks, Compose, System
  - Active tab persisted across reloads; optional fixed startup tab
  - Image table scrolls within the viewport with a sticky header
  - Removed always-empty CPU/Memory gauges in favor of a compact host stat row

- **Persistent filter and sort settings**
  - Image table sort control: status priority (default), image name, recently checked
  - Image filter, container filter, and "show stopped" toggle persist across reloads

- **Preferences menu**
  - Dashboard: auto-refresh frequency, startup tab, recent jobs count
  - Scheduled checks: registry check interval (server-side, persisted to `app_settings.json`, survives restarts)
  - Update behavior: auto-recreate after pull
  - Appearance: theme (light/dark/system)

- **Remote host persistence API**
  - `GET/POST /api/instances/remote` reads and replaces the remote host list in `REMOTE_INSTANCES_FILE`
  - Add/edit/remove remote hosts from the UI now persists via Save

- **Configuration API**
  - `GET/POST /api/config` now reports and accepts `check_interval_minutes`, rescheduling the scheduled check job at runtime

### Changed

- OS package updates and container start/stop/restart/inspect now run against the selected instance instead of always the local host
- Action modals can no longer hang on failure: all action calls check response status and surface server error messages
- Theme "System" preference now persists correctly across reloads

### Fixed

- Notifications and Preferences modals failed to open (undefined `NOTIFY_*` and `AUTO_RECREATE_AFTER_PULL` references); values now load from the API
- Image and container filter buttons reset each other's active state across tabs
- Performance menu auto-refresh toggled the wrong item's label
- Changing auto-recreate only updated one module's copy; `/api/status` and `/api/config` now report the current value
- Duplicate `formatBytes` definition and various dead code removed

---

## [v0.4.0] — 2026-09-18

### Added

- **Dense compact UI layout**
  - Reduced base font size from 15px to 14px for better density
  - Removed card borders/backgrounds and reduced all margins
  - Combined Hosts + KPIs + Host Resources into unified grid layout
  - Activity Log now collapsible (hidden by default to save space)
  - Reduced table cell padding and all spacing throughout
  - Reduced gauge sizes from 100px to 80px
  - All UI elements consistently reduced in size

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

- **Backend refactor and API extraction**
  - `app.py` refactored to delegate responsibilities across new modules:
    - `config.py` for configuration loading
    - `docker_utils.py` for compose scanning, image checks, and Docker compose operations
    - `jobs.py` for background job state and operation logging
    - `notifier.py` for webhook/MQTT/email notification delivery
    - `api.py` for Flask route handlers
  - Eliminated monolithic route handling inside `app.py`
  - `/api/*` endpoints are now centralized in `api.py` for cleaner separation of concerns

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

## [Unreleased]

### Added

### Changed

### Fixed

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