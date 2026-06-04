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
