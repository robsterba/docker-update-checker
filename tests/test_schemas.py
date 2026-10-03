"""Tests for pydantic request schemas."""
import pytest
from pydantic import ValidationError

from schemas import (
    BulkUpdateRequest,
    ComposeFileContentRequest,
    ComposeFileValidateRequest,
    ComposeRecreateRequest,
    ConfigUpdateRequest,
    ImageUpdateRequest,
    PruneRequest,
    StackActionRequest,
    StackBulkActionRequest,
)


class TestConfigUpdateRequest:
    def test_bool_passthrough(self):
        assert ConfigUpdateRequest.model_validate({"auto_recreate": True}).auto_recreate is True

    def test_string_parsing(self):
        for raw, expected in [("true", True), ("1", True), ("yes", True), ("on", True),
                              ("false", False), ("no", False), ("off", False), ("0", False)]:
            model = ConfigUpdateRequest.model_validate({"auto_recreate": raw})
            assert model.auto_recreate is expected, raw

    def test_unparseable_string_rejected(self):
        for raw in ("", "junk"):
            with pytest.raises(ValidationError):
                ConfigUpdateRequest.model_validate({"auto_recreate": raw})

    def test_int_parsing(self):
        assert ConfigUpdateRequest.model_validate({"auto_recreate": 1}).auto_recreate is True
        assert ConfigUpdateRequest.model_validate({"auto_recreate": 0}).auto_recreate is False

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            ConfigUpdateRequest.model_validate({})


class TestImageUpdateRequest:
    def test_auto_recreate_optional(self):
        assert ImageUpdateRequest.model_validate({}).auto_recreate is None
        assert ImageUpdateRequest.model_validate({"auto_recreate": "yes"}).auto_recreate is True


class TestBulkUpdateRequest:
    def test_defaults(self):
        model = BulkUpdateRequest.model_validate({})
        assert model.stack is None
        assert model.auto_recreate is None

    def test_empty_stack_rejected(self):
        with pytest.raises(ValidationError):
            BulkUpdateRequest.model_validate({"stack": "   "})

    def test_stack_is_stripped(self):
        assert BulkUpdateRequest.model_validate({"stack": " media "}).stack == "media"


class TestComposeRequests:
    def test_recreate_requires_path(self):
        assert ComposeRecreateRequest.model_validate({"compose_path": "media/compose.yaml"}).compose_path == "media/compose.yaml"
        with pytest.raises(ValidationError):
            ComposeRecreateRequest.model_validate({"compose_path": "   "})

    def test_content_must_be_dict(self):
        with pytest.raises(ValidationError):
            ComposeFileContentRequest.model_validate({"content": ["nope"]})

    def test_validate_content_optional(self):
        assert ComposeFileValidateRequest.model_validate({}).content is None
        with pytest.raises(ValidationError):
            ComposeFileValidateRequest.model_validate({"content": "not-a-dict"})


class TestPruneRequest:
    def test_all_parsing(self):
        assert PruneRequest.model_validate({}).all is False
        assert PruneRequest.model_validate({"all": True}).all is True
        assert PruneRequest.model_validate({"all": "true"}).all is True
        assert PruneRequest.model_validate({"all": None}).all is False


class TestStackRequests:
    def test_timeout_must_be_positive(self):
        assert StackActionRequest.model_validate({"timeout": 30}).timeout == 30
        with pytest.raises(ValidationError):
            StackActionRequest.model_validate({"timeout": 0})
        with pytest.raises(ValidationError):
            StackActionRequest.model_validate({"timeout": -1})

    def test_bulk_action_requires_valid_action(self):
        model = StackBulkActionRequest.model_validate(
            {"stack_names": ["media"], "action": "up"}
        )
        assert model.action == "up"
        for bad in ("pause", "stop", ""):
            with pytest.raises(ValidationError):
                StackBulkActionRequest.model_validate({"stack_names": ["media"], "action": bad})

    def test_bulk_action_requires_non_empty_stack_list(self):
        with pytest.raises(ValidationError):
            StackBulkActionRequest.model_validate({"stack_names": [], "action": "up"})
        with pytest.raises(ValidationError):
            StackBulkActionRequest.model_validate({"stack_names": ["  "], "action": "up"})
