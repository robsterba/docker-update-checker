# Changelog

All notable changes to **Docker Update Checker** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to a manual-release model (no automated version numbering yet).

---

## [v1.7.2] — 2026-10-03

### Fixed

- **taskd parent tasks now show the real host machine name** instead of the app container's ID. The integration's host identity defaults to the Docker daemon's reported host name (new `get_docker_host_name()` in `docker_utils.py`), falling back to the container hostname only when the daemon cannot be reached; `TASKD_HOST_LABEL` still overrides both. Previously, when no label was configured, `socket.gethostname()` returned the container ID (e.g. `Container updates: 083b9e66e922`), which identified nothing. The detected name is memoized so syncs never re-query the daemon

---

## [v1.7.1] — 2026-10-03

### Changed

- **Overview layout: Metrics now sits above Hosts and spans the full width**, compressed into a two-row band. Row one is the five image KPIs (Total Images, Up to Date, Updates Available, Unknown / Error, Stacks); row two is the six host stats (CPU Cores, Memory, Running, Stopped, Docker, OS). KPI values and padding are tightened so the band stays short, and both rows reflow to three columns below 980px and two below 760px. The Hosts instance grid moves below the band and also spans the full width

---

## [v1.7.0] — 2026-10-03

### Added

- **Native taskd integration** (`taskd.py`): after every scan, outdated images are synced into a [taskd](https://github.com/robsterba/taskd) instance — one long-lived parent task per host (`Container updates: <host>`) with one subtask per outdated image. Subtasks carry the local/remote digests, stacks, and compose files in their description, are refreshed in place on each scan, reopen automatically if completed while the image is still outdated, and complete automatically once the image is up to date again (or no longer appears in any compose file). Images with registry errors or unknown check status are never auto-completed, and a scan that finds no images at all skips the completion pass — a broken compose scan can never mass-close tasks. A taskd outage is logged and retried on the next check; it never fails the scan
- **taskd settings UI** (Settings → taskd Integration): enable toggle, URL, timeout, host label, tags, and source, plus a **Test Connection** button that probes taskd's health endpoint and reports latency
- **API endpoints** for the integration: `GET`/`POST /api/config/taskd` (runtime-persisted settings, validated and normalized server-side) and `POST /api/taskd/test` (connection test with optional `url`/`timeout` overrides)
- **`TASKD_*` configuration** (`TASKD_ENABLED`, `TASKD_URL`, `TASKD_TIMEOUT`, `TASKD_HOST_LABEL`, `TASKD_TAGS`, `TASKD_SOURCE`): environment defaults, overridable at runtime via the UI or settings file (file wins, matching notification settings precedence)
- **Test suite extended to 202 tests** (`tests/test_taskd.py`): settings merge and normalization, sync gating and locking, the full reconcile lifecycle (dedupe, refresh, reopen, safe-complete, user-created and archived subtask handling, parent matching), the API endpoints, and scan-survives-taskd-outage coverage

### Notes

- taskd settings are resolved at sync time, so changes made in the UI take effect on the next scan without a restart — unlike notification settings, which still apply only from the environment (pre-existing behavior)
- Each docker-update-checker instance syncs only its own host's results; enable the integration on every instance that should own a parent task in taskd
- `taskd_settings.json` is gitignored alongside the other runtime settings files

---

## [v1.6.1] — 2026-10-03

### Fixed

- **The scanner no longer descends into excluded directories** when finding compose files. New `COMPOSE_EXCLUDE_DIRS` setting (comma-separated directory names, default: `tests`) prunes directories in place during the walk, so a deployment that mounts the repository itself under `COMPOSE_ROOT` no longer discovers the app's own test fixture compose files and tries to pull their images. The exclusion applies everywhere scanning happens: update checks, stack lists, and the compose file browser.

---

## [v1.6.0] — 2026-10-03

### Added

- **Test suite** (`tests/`, run with `python -m pytest`): 165 tests covering image reference parsing, compose file discovery/validation, the registry digest flow (HTTP fully mocked), job tracking, notification gating/throttling/batching, remote instance handling, the instance proxy, request schemas, and the Flask API surface via the test client — no Docker daemon or network access required
- **CI** (`.github/workflows/ci.yml`): GitHub Actions running `ruff check` and `pytest` on Python 3.12 for pushes and pull requests
- **Lint configuration** (`pyproject.toml`): ruff (E, W, F, B rules, 130-char lines) and pytest settings
- **`requirements-dev.txt`** with pytest, requests-mock, and ruff
- **`style.md` is now tracked in git** (previously ignored by the `*.md` gitignore rule; the theme spec is referenced by release history and needed on every machine)

### Changed

- **Import refactor: `app.py` is now a thin entry point.** All business logic (orchestration, proxying, settings, jobs) moved to a new `services.py`, which owns the Flask app object and the scheduler. `api.py` imports from `services` instead of `app`, and the `sys.modules["app"]` circular-import shim is gone — the import graph is now acyclic and importing any module stays side-effect-free
- **`AUTO_RECREATE_AFTER_PULL` is read from `config` at call time** instead of being copied into each module and patched in three places at runtime
- Duplicate `logging.basicConfig` removed (config.py is the single place logging is configured)
- `paho-mqtt` pinned to 2.1.0 (all other dependencies were already pinned)
- `.gitattributes` added: LF normalization for text files, LF enforcement for shell/systemd files
- `.env.example`: `REMOTE_INSTANCES_FILE` no longer defaults to the example file, and a note clarifies that the container does not read `.env` directly (Compose interpolation only)
- **readme rewritten**: version reference sourced from `VERSION`/`CHANGELOG`, stale v0.3/v0.4 feature sections removed, clone URL fixed, config defaults verified against `config.py`, complete API endpoint table, new Security section (unauthenticated API, socket mount semantics), `.env` semantics clarified, project structure updated, Development section added
- Line endings normalized to LF across Python, compose, Dockerfile, and script files

### Fixed

- **Deadlock in notification batching**: `send_notification` called `_schedule_batch_timer` while holding `_batch_lock`, which then tried to re-acquire the non-reentrant lock — any batched notification (pull/recreate/bulk with `NOTIFY_SUMMARY_ENABLED=true` and `NOTIFY_BATCH_WINDOW>0`, the defaults) hung its worker thread forever once notifications were enabled
- **`POST /api/compose/recreate` crashed with `NameError`**: `subprocess` was never imported in `api.py`
- **Local instance proxy `stacks/all` and `update/<image>` paths crashed with `NameError`**: `get_all_stacks` / `api_update_image` were not imported
- **Self-update and OS-update notifications never sent**: callers passed `data=` to `send_notification`, whose signature takes `extra=` (also caused `NameError` on the undefined `log` in the API's exception handlers)
- Unused imports removed across `api.py`, `app.py`, `notifier.py`, and `scripts/os_update_agent.py`

### Removed

- Stale local-only working documents (Project Overview, Technical Architecture, Documentation Summary, Code Review Findings, Obsidian import guide), all dated to v1.3.0 and superseded by the readme and changelog

---

## [v1.5.0] — 2026-10-02

### Added

- **AMOLED / true black theme** (style.md spec section 2): third token block on a true-black page with near-black surfaces, keeping the icy-blue accents and brightened status neutrals of the dusk-blue dark theme
- **Preferred dark variant setting** (Preferences → Appearance): choose "Blue (default)" or "AMOLED Black"; applies immediately while a dark theme is active

### Changed

- **Theme switching conforms to the standard model (spec section 10)**: the header toggle now flips between light and the preferred dark variant only; without a saved theme, the OS preference decides light vs dark on first visit (the old "System" option was removed — first-load OS detection remains)
- **Version badge moved beside the app title (spec section 7)**: sits immediately right of "Docker Update Checker" in the header instead of in the actions cluster, restyled per the spec (0.85rem, 4px radius, surface-alt fill, no border, no monospace)

---

## [v1.4.0] — 2026-09-27

### Changed

- **Dashboard restyle to the updated style.md (Standard Web Theme Spec, blue-forward palette)**
  - Both token blocks (light + dark) updated verbatim to the new palette: Platinum (`#E7ECEF`) day page with Dusk Blue (`#274C77`) text/accents, night theme inverted to a Dusk Blue page with Platinum text and Icy Blue (`#A3CEF1`) accent fills; Steel Blue (`#6096BA`) borders/hover; Grey Olive (`#8B8C89`) secondary day text
  - New `--highlight` / `--highlight-soft` tokens included per the spec (selected rows, info chips)
  - Base font size increased from 14px to 16px, scaling the whole rem-based interface up for readability
  - Smallest font sizes (filter buttons, last-check, health/version badges, resource labels) raised from .6–.68rem to a .7rem floor
- **Versioning now driven by a single `VERSION` file at the repo root** (same model as taskd)
  - New `version.py` reads the `VERSION` file with path candidates and a fallback; `config.py` imports `VERSION` from it instead of hard-coding the string
  - GUI badge and `/api/version` reflect the file automatically; the Dockerfile needs no change (`COPY . .` ships the file)

### Fixed

- `app.py` no longer starts the background scheduler and the startup image check at import time; that work moved into `start_background_workers()`, called only when `app.py` runs as the entry point. Importing `app`/`api` (tooling, tests, future WSGI servers) is now side-effect-free. The `BackgroundScheduler` object stays module-level so the check-interval API can still reschedule jobs

---

## [v1.3.0] — 2026-09-26

### Changed

- **Dashboard restyle to fully conform to style.md (Standard Web Theme Spec)**
  - Both token blocks (light + dark) are now included verbatim under the spec's token names (`--bg-page`, `--bg-surface`, `--accent`, `--text-primary`, ...); all component rules and inline styles were migrated off the previous renamed tokens (`--bg`, `--surface`, `--muted`, ...)
  - Base element styles per the spec: system font stack, underlined links, `hr`/divider rule, `code`/`pre` on the inset surface with the spec monospace stack (`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`), and form inputs (`input`/`select`/`textarea`) on `--bg-surface-alt` with `--border-default` and 6px radius
  - Badge tint recipe per the spec: `color-mix` tints at 15% (informational) and 18% (severity) against `--success` / `--warning` / `--danger` / `--info`; badges and chips are 12px radius pills, `not_pulled`/`unknown` use the muted (surface-alt) treatment
  - Buttons per the spec: 6px radius, accent fill reserved for the primary action with hover/pressed states, outline treatment for default/secondary buttons, and a 1px press-down on `:active`
  - Cards are real panels now: `--bg-surface` background, subtle border, 8px radius, `--shadow-sm`; other card-like surfaces (jobs, container items, config sections, compose file cards) normalized to 8px radius
  - Tables use the surface background with `--bg-surface-alt` headers and row hover
  - Theme toggle styled per the spec's outline button recipe

### Removed

- Google Fonts (Inter) dependency; the dashboard now uses the spec's system and monospace font stacks, removing external font requests

---

## [v1.2.1] — 2026-09-26

### Fixed

- Dashboard stuck on "Loading host status…" with no data loading at all
  - `init()` threw `ReferenceError: STORAGE_IMAGE_FILTER_KEY is not defined` in `initFilters()`, aborting startup before host instances or any dashboard data could load
  - The revert/reapply in v1.1.x restored filter-persistence code whose four localStorage key constants (`STORAGE_IMAGE_FILTER_KEY`, `STORAGE_IMAGE_SORT_KEY`, `STORAGE_CONTAINER_FILTER_KEY`, `STORAGE_SHOW_STOPPED_KEY`) were never defined; they are now declared alongside the other storage keys

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