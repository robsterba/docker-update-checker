"""
Pydantic schemas for API request validation.

This module provides input validation for all API endpoints to prevent
injection attacks, type confusion, and invalid data.
"""

from typing import Optional
from pydantic import BaseModel, field_validator


class ConfigUpdateRequest(BaseModel):
    """Request body for /api/config endpoint."""
    auto_recreate: bool
    
    @field_validator('auto_recreate')
    @classmethod
    def parse_auto_recreate(cls, v):
        """Parse auto_recreate from various input types."""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, int):
            return bool(v)
        return False


class ImageUpdateRequest(BaseModel):
    """Request body for /api/update/<image_ref> endpoint."""
    auto_recreate: Optional[bool] = None
    
    @field_validator('auto_recreate')
    @classmethod
    def parse_auto_recreate(cls, v):
        """Parse auto_recreate from various input types."""
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, int):
            return bool(v)
        return False


class BulkUpdateRequest(BaseModel):
    """Request body for /api/bulk/update endpoint."""
    stack: Optional[str] = None
    auto_recreate: Optional[bool] = None
    
    @field_validator('stack')
    @classmethod
    def validate_stack(cls, v):
        """Validate stack name is a non-empty string if provided."""
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("stack must be a non-empty string")
        return v
    
    @field_validator('auto_recreate')
    @classmethod
    def parse_auto_recreate(cls, v):
        """Parse auto_recreate from various input types."""
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, int):
            return bool(v)
        return False


class ComposeRecreateRequest(BaseModel):
    """Request body for /api/compose/recreate endpoint."""
    compose_path: str
    
    @field_validator('compose_path')
    @classmethod
    def validate_compose_path(cls, v):
        """Validate compose_path is a non-empty string."""
        v = v.strip()
        if not v:
            raise ValueError("compose_path is required")
        return v


class PruneRequest(BaseModel):
    """Request body for prune endpoints (/api/prune/volumes, /api/prune/images)."""
    all: Optional[bool] = False
    
    @field_validator('all')
    @classmethod
    def parse_all(cls, v):
        """Parse 'all' flag from various input types."""
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, int):
            return bool(v)
        return False


class InstanceProxyRequest(BaseModel):
    """Request body for /api/instances/<instance_id>/<path:proxy_path> endpoint."""
    pass  # Proxy requests pass through without validation


# -- Phase 2: Compose File Management Schemas --


class ComposeFileListRequest(BaseModel):
    """Request query parameters for /api/compose/files endpoint."""
    project: Optional[str] = None
    
    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        """Validate project name is a non-empty string if provided."""
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("project must be a non-empty string")
        return v


class ComposeFileContentRequest(BaseModel):
    """Request body for /api/compose/files/<path> PUT endpoint."""
    content: dict
    backup: Optional[bool] = True
    
    @field_validator('content')
    @classmethod
    def validate_content(cls, v):
        """Validate content is a dictionary."""
        if not isinstance(v, dict):
            raise ValueError("content must be a dictionary (parsed YAML)")
        return v


class ComposeFileValidateRequest(BaseModel):
    """Request body for /api/compose/files/<path>/validate endpoint."""
    content: Optional[dict] = None
    
    @field_validator('content')
    @classmethod
    def validate_content(cls, v):
        """Validate content is a dictionary if provided."""
        if v is not None and not isinstance(v, dict):
            raise ValueError("content must be a dictionary (parsed YAML)")
        return v


class StackActionRequest(BaseModel):
    """Request body for stack actions (/api/stacks/<name>/up, down, restart)."""
    timeout: Optional[int] = None
    
    @field_validator('timeout')
    @classmethod
    def validate_timeout(cls, v):
        """Validate timeout is a positive integer if provided."""
        if v is not None:
            if not isinstance(v, int) or v <= 0:
                raise ValueError("timeout must be a positive integer")
        return v


class StackBulkActionRequest(BaseModel):
    """Request body for bulk stack operations."""
    stack_names: list[str]
    action: str  # 'up', 'down', 'restart'
    timeout: Optional[int] = None
    
    @field_validator('stack_names')
    @classmethod
    def validate_stack_names(cls, v):
        """Validate stack_names is a non-empty list."""
        if not isinstance(v, list) or len(v) == 0:
            raise ValueError("stack_names must be a non-empty list")
        for name in v:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Stack name '{name}' is invalid")
        return v
    
    @field_validator('action')
    @classmethod
    def validate_action(cls, v):
        """Validate action is one of the allowed values."""
        if v not in ('up', 'down', 'restart'):
            raise ValueError(f"action must be 'up', 'down', or 'restart', got '{v}'")
        return v
    
    @field_validator('timeout')
    @classmethod
    def validate_timeout(cls, v):
        """Validate timeout is a positive integer if provided."""
        if v is not None:
            if not isinstance(v, int) or v <= 0:
                raise ValueError("timeout must be a positive integer")
        return v


class ComposeFileRenameRequest(BaseModel):
    """Request body for renaming/moving a compose file."""
    new_path: str
    
    @field_validator('new_path')
    @classmethod
    def validate_new_path(cls, v):
        """Validate new_path is a non-empty string."""
        v = v.strip()
        if not v:
            raise ValueError("new_path is required")
        return v
