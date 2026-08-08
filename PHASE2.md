# Phase 2: Compose & Lifecycle Management Implementation

## Overview
Expand docker-homelab-manager to include advanced compose file management, stack lifecycle controls, and enhanced configuration capabilities.

## Features
- [ ] Compose file viewer/editor - web-based YAML editing with validation
- [ ] Stack start/stop - full compose up/down with environment variable support
- [ ] Stack dependency mapping - visualize container relationships
- [ ] Advanced bulk operations - apply actions across multiple stacks

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/compose/files` | List all compose files (existing)
| GET | `/api/compose/files/{path}` | Get compose file content
| PUT | `/api/compose/files/{path}` | Update compose file content
| POST | `/api/compose/files/{path}/validate` | Validate compose file syntax
| POST | `/api/stacks/{name}/up` | Start stack (docker compose up)
| POST | `/api/stacks/{name}/down` | Stop stack (docker compose down)
| POST | `/api/stacks/{name}/restart` | Restart entire stack
| GET | `/api/stacks/{name}/dependencies` | Get stack dependency graph

## File Changes Required
- [ ] `api.py` - new compose management endpoints
- [ ] `docker_utils.py` - compose file helpers
- [ ] `static/index.html` - new compose editor UI
- [ ] New `compose_editor.js` - optional, for rich YAML editing

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
- New "Compose Files" tab/section in dashboard
- Stack action buttons (Up/Down/Restart)
- Visual dependency graph for stacks
- Diff viewer for compose file changes

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