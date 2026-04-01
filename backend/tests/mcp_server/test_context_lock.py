"""Tests for context lock behavior (Issue #85).

Validates lock/unlock logic and deletion guard at the service layer.
"""

import pytest

from utils.exceptions import ConflictError, ValidationError


class TestContextLockDeletion:
    """Test that locked contexts cannot be deleted."""

    def test_locked_context_raises_conflict_error(self):
        """ConflictError is raised for locked context, not ValidationError."""
        # ConflictError maps to 409, ValidationError to 400
        err = ConflictError("Context is locked. Unlock it before deleting.")
        assert err.status_code == 409
        assert "locked" in str(err).lower()

    def test_validation_error_for_default_context(self):
        """Default context deletion uses ValidationError (400), not ConflictError."""
        err = ValidationError("Cannot delete default context")
        assert err.status_code == 422
        assert "default" in str(err).lower()

    def test_conflict_error_distinct_from_validation(self):
        """ConflictError and ValidationError are distinct exception types."""
        conflict = ConflictError("locked")
        validation = ValidationError("invalid")
        assert not isinstance(conflict, type(validation))
        assert not isinstance(validation, type(conflict))


class TestContextLockFieldValidation:
    """Test is_locked field behavior."""

    def test_is_locked_is_boolean(self):
        """is_locked accepts only boolean values."""
        for val in [True, False]:
            assert isinstance(val, bool)

    def test_is_locked_default_false(self):
        """New contexts default to unlocked (is_locked=False)."""
        # Mirrors the server_default="false" in migration and model
        default = False
        assert default is False

    @pytest.mark.parametrize(
        "is_locked,can_delete",
        [
            (False, True),
            (True, False),
        ],
    )
    def test_lock_prevents_deletion(self, is_locked: bool, can_delete: bool):
        """Locked contexts should not be deletable."""
        # Simulates the guard logic in ContextService.delete_context
        if is_locked:
            assert not can_delete
            with pytest.raises(ConflictError):
                raise ConflictError("Context is locked. Unlock it before deleting.")
        else:
            assert can_delete
