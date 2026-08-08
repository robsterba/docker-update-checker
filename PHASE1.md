# Phase 1: Container Monitoring Implementation

## Overview
Expand docker-update-checker to include container lifecycle management and resource monitoring capabilities.

## Features
- [ ] Container list API - list all containers with status
- [ ] Basic resource monitoring - CPU/memory/disk per container
- [ ] Start/stop/restart endpoints - container lifecycle controls
- [ ] Health check visualization - dashboard for health status

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/containers` | List all containers with basic info |
| GET | `/api/containers/{id}/resources` | Resource usage for specific container |
| GET | `/api/host/resources` | Aggregate resource usage for host |
| POST | `/api/containers/{id}/start` | Start a stopped container |
| POST | `/api/containers/{id}/stop` | Stop a running container |
| POST | `/api/containers/{id}/restart` | Restart a container |

## File Changes Required
- [ ] `api.py` - new endpoint handlers
- [ ] `docker_utils.py` - container inspection helpers
- [ ] `static/index.html` - new UI sections for container management
- [ ] `jobs.py` - optional, for async container operations

## Design Decisions
- **Polling interval**: 30 seconds for resource data (configurable)
- **Authentication**: Use existing Docker socket permissions (no change for Phase 1)
- **Error handling**: Return HTTP 400/500 with descriptive messages
- **Container filtering**: Support filter by status, name pattern, stack

## Dependencies
- Existing Docker SDK connection already handles container access
- No new Python packages required

## UI Integration
- New "Containers" tab/section in dashboard
- Resource usage charts (simple text-based for Phase 1)
- Container action buttons (start/stop/restart)
- Health status indicators

## Testing Checklist
- [ ] List containers works on local host
- [ ] List containers works on remote instances
- [ ] Start/stop/restart work correctly
- [ ] Resource data refreshes properly
- [ ] Error states display correctly
- [ ] Large number of containers (50+) doesn't break UI

## Notes
- Focus on readability over fancy visualizations
- Keep existing update-checking functionality intact
- Prioritize functionality that works across both local and remote instances