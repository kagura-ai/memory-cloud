"""Tests for MCP tool helper functions."""

import json
from uuid import UUID, uuid4

import pytest

from mcp_server.tools._helpers import (
    _context_response_fields,
    _error_response,
    _resolve_context_id,
    _success_response,
    _validate_memory_id,
)


class TestSuccessResponse:
    """Test _success_response helper."""

    def test_basic_response(self):
        """Test basic success response format."""
        result = _success_response(message="ok")
        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["message"] == "ok"

    def test_multiple_fields(self):
        """Test success response with multiple fields."""
        result = _success_response(memory_id="abc", count=5)
        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["memory_id"] == "abc"
        assert data["count"] == 5


class TestErrorResponse:
    """Test _error_response helper."""

    def test_basic_error(self):
        """Test basic error response format."""
        result = _error_response("not_found", "Memory not found")
        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "not_found"
        assert data["message"] == "Memory not found"

    def test_error_with_extra_fields(self):
        """Test error response with extra fields."""
        result = _error_response("invalid", "Bad input", help="Use recall() first")
        data = json.loads(result[0].text)
        assert data["help"] == "Use recall() first"


class TestContextResponseFields:
    """Test _context_response_fields helper."""

    def test_none_context(self):
        """Test with None context."""
        fields = _context_response_fields(None)
        assert fields["context_id"] is None
        assert fields["context_name"] is None

    def test_valid_context(self):
        """Test with valid context object."""
        ctx = type(
            "Ctx",
            (),
            {
                "id": uuid4(),
                "name": "test-ctx",
                "display_name": "Test Context",
                "is_private": False,
                "is_locked": False,
            },
        )()
        fields = _context_response_fields(ctx)
        assert fields["context_id"] == str(ctx.id)
        assert fields["context_name"] == "test-ctx"
        assert fields["context_display_name"] == "Test Context"
        assert fields["context_is_private"] is False
        assert fields["context_is_locked"] is False


class TestResolveContextId:
    """Test _resolve_context_id helper."""

    def test_valid_uuid(self):
        """Test with valid UUID string."""
        uid = str(uuid4())
        result = _resolve_context_id(uid)
        assert isinstance(result, UUID)
        assert str(result) == uid

    def test_invalid_uuid(self):
        """Test with invalid UUID string."""
        with pytest.raises(ValueError, match="Invalid context_id format"):
            _resolve_context_id("not-a-uuid")

    def test_empty_string(self):
        """Test with empty string."""
        with pytest.raises(ValueError, match="Invalid context_id format"):
            _resolve_context_id("")

    def test_none_value(self):
        """Test with None."""
        with pytest.raises(ValueError, match="Invalid context_id format"):
            _resolve_context_id(None)


class TestValidateMemoryId:
    """Test _validate_memory_id helper."""

    def test_valid_memory_id(self):
        """Test with valid memory_id."""
        uid = str(uuid4())
        result, error = _validate_memory_id({"memory_id": uid}, "reference")
        assert result == UUID(uid)
        assert error is None

    def test_missing_memory_id(self):
        """Test with missing memory_id."""
        result, error = _validate_memory_id({}, "reference")
        assert result is None
        assert error is not None
        data = json.loads(error[0].text)
        assert data["error"] == "memory_id_required"

    def test_invalid_memory_id(self):
        """Test with invalid memory_id format."""
        result, error = _validate_memory_id({"memory_id": "bad"}, "reference")
        assert result is None
        assert error is not None
        data = json.loads(error[0].text)
        assert data["error"] == "invalid_memory_id_format"
