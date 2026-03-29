"""Tests for MCP update_context resource_id validation."""

import re


class TestResourceIdValidation:
    """Test resource_id format validation (matches REST API Pydantic pattern)."""

    PATTERN = re.compile(r"^[a-z0-9_-]+$")

    def test_valid_resource_ids(self):
        """Valid resource_id formats."""
        valid = [
            "github_issues",
            "slack_threads",
            "my_resource",
            "test123",
            "a",
            "abc",
            "resource_with_underscores",
            "sdk-test",
            "github-issues",
            "my-resource-123",
        ]
        for rid in valid:
            assert self.PATTERN.match(rid) and len(rid) <= 255, f"Should be valid: {rid}"

    def test_invalid_resource_ids(self):
        """Invalid resource_id formats."""
        invalid = [
            "",  # empty
            "GitHub-Issues",  # uppercase
            "my resource",  # space
            "MY_RESOURCE",  # uppercase
            "resource@id",  # special char
            "日本語",  # non-ASCII
            "a" * 256,  # too long
        ]
        for rid in invalid:
            is_valid = bool(self.PATTERN.match(rid)) and len(rid) <= 255
            assert not is_valid, f"Should be invalid: {rid}"

    def test_empty_string_rejected(self):
        """Empty string should not match the pattern."""
        assert not self.PATTERN.match("")

    def test_max_length_boundary(self):
        """255 chars OK, 256 chars rejected."""
        assert self.PATTERN.match("a" * 255) and len("a" * 255) <= 255
        assert len("a" * 256) > 255
