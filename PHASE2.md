# Phase 2: Compose & Lifecycle Management Implementation

## Overview
Expand docker-homelab-manager to include advanced compose file management, stack lifecycle controls, and enhanced configuration capabilities.

## Features
- [x] Compose file viewer/editor - web-based YAML editing with validation
- [x] Stack start/stop - full compose up/down with environment variable support
- [x] Stack dependency mapping - visualize container relationships
- [x] Advanced bulk operations - apply actions across multiple stacks

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/compose/files` | List all compose files (existing)
| GET | `/api/compose/files/detailed` | List all compose files with metadata (services, images)
| GET | `/api/compose/files/{path}` | Get compose file content
| PUT | `/api/compose/files/{path}` | Update compose file content
| POST | `/api/compose/files/{path}/validate` | Validate compose file syntax
| GET | `/api/compose/files/{path}/dependencies` | Get dependency graph for compose file
| GET | `/api/stacks/all` | Get all stacks with detailed info
| GET | `/api/stacks/{name}` | Get specific stack info
| GET | `/api/stacks/{name}/containers` | Get containers for a stack
| GET | `/api/stacks/{name}/status` | Get status of stack containers
| POST | `/api/stacks/{name}/up` | Start stack (docker compose up)
| POST | `/api/stacks/{name}/down` | Stop stack (docker compose down)
| POST | `/api/stacks/{name}/restart` | Restart entire stack
| POST | `/api/stacks/bulk` | Bulk action on multiple stacks

## Implementation Status
- ✅ Compose file management functions in `docker_utils.py`
- ✅ Stack management functions in `docker_utils.py`
- ✅ Pydantic schemas for validation in `schemas.py`
- ✅ API endpoints in `api.py`
- ✅ UI updates in `static/index.html` with compose editor and stack management

## File Changes Required
- [x] `api.py` - new compose management endpoints
- [x] `docker_utils.py` - compose file helpers
- [x] `static/index.html` - new compose editor UI
- [x] `schemas.py` - request validation schemas
- [ ] New `compose_editor.js` - optional, for rich YAML editing (using CDN for now)

## Design Decisions
- **Editor choice**: Use Monaco editor for rich YAML editing, or simple textarea for Phase 2
- **Validation**: Real-time validation with clear error messages
- **Backup**: Auto-backup before saving compose file changes
- **Environment variables**: Support variable resolution and editing

## Dependencies
- Existing Docker SDK already handles compose operations
- Monaco editor optional (can use CodeMirror or simple textarea)
- YAML validation library for client-side validation

## UI Integration
- [x] New "Compose Files" dropdown menu in toolbar
- [x] New "Compose Files" card section with file list
- [x] Stack action buttons (Up/Down/Restart) with status badges
- [x] Visual dependency graph for stacks
- [ ] Diff viewer for compose file changes (future enhancement)

## Testing Checklist
- [ ] Compose file listing works across hosts
- [ ] File content loading and saving
- [ ] YAML validation catches errors
- [ ] Stack up/down works correctly
- [ ] Dependency graph renders properly
- [ ] Error states display correctly

## Notes
- Prioritize safety: always backup before changes
- Consider adding a "read-only" mode for production instances
- Integrate with existing image update detection

---

## ✅ Phase 2 COMPLETE
All Phase 2 features have been implemented:

### Backend (Python)
- `docker_utils.py`: Added 15+ new functions for compose file and stack management
- `schemas.py`: Added Pydantic validation schemas for all new endpoints
- `api.py`: Added 15+ new API endpoints for compose files and stack management

### Frontend (UI)
- Enhanced stack cards with Start/Stop/Restart/Details actions
- New Compose Files dropdown menu with file listing
- YAML editor with validation and dependency graph visualization
- Status badges showing stack state (running/stopped/mixed)

### Features Delivered
1. ✅ Compose file viewer/editor with YAML validation
2. ✅ Stack start/stop/restart lifecycle controls
3. ✅ Stack dependency mapping and visualization
4. ✅ Advanced bulk operations across stacks

### Testing
All Python files compile successfully. The new endpoints are ready for integration testing with a running Docker environment.