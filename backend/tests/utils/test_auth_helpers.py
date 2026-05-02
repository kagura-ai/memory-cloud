"""Tests for utils/auth_helpers.py.

Pure utility — no DB or external services required.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from utils.auth_helpers import (
    get_user_email,
    get_user_id,
    get_user_role,
    is_admin,
    verify_ownership,
)


class TestGetUserId:
    def test_dict_with_user_id(self):
        user = {"user_id": "abc123", "email": "user@example.com"}
        assert get_user_id(user) == "abc123"

    def test_dict_with_sub(self):
        user = {"sub": "oauth-sub-456", "email": "user@example.com"}
        assert get_user_id(user) == "oauth-sub-456"

    def test_dict_with_id(self):
        user = {"id": "orm-id-789", "email": "user@example.com"}
        assert get_user_id(user) == "orm-id-789"

    def test_dict_missing_keys_raises_401(self):
        user = {"email": "user@example.com"}
        with pytest.raises(HTTPException) as exc:
            get_user_id(user)
        assert exc.value.status_code == 401
        assert "User ID not found" in exc.value.detail

    def test_userlike_object(self):
        user = MagicMock()
        user.id = "obj-id-111"
        user.email = "obj@example.com"
        user.role = "member"
        assert get_user_id(user) == "obj-id-111"

    def test_arbitrary_object_raises_401(self):
        user = object()
        with pytest.raises(HTTPException) as exc:
            get_user_id(user)
        assert exc.value.status_code == 401


class TestGetUserEmail:
    def test_dict_with_email(self):
        user = {"user_id": "abc", "email": "user@example.com"}
        assert get_user_email(user) == "user@example.com"

    def test_dict_without_email(self):
        user = {"user_id": "abc"}
        assert get_user_email(user) is None

    def test_userlike_object(self):
        user = MagicMock()
        user.id = "obj-id"
        user.email = "obj@example.com"
        user.role = "member"
        assert get_user_email(user) == "obj@example.com"

    def test_userlike_object_none_email(self):
        user = MagicMock()
        user.id = "obj-id"
        user.email = None
        user.role = "member"
        assert get_user_email(user) is None

    def test_arbitrary_object_returns_none(self):
        assert get_user_email(object()) is None


class TestGetUserRole:
    def test_dict_with_role(self):
        user = {"user_id": "abc", "role": "admin"}
        assert get_user_role(user) == "admin"

    def test_dict_defaults_to_user(self):
        user = {"user_id": "abc"}
        assert get_user_role(user) == "user"

    def test_userlike_object(self):
        user = MagicMock()
        user.id = "obj-id"
        user.email = "obj@example.com"
        user.role = "owner"
        assert get_user_role(user) == "owner"

    def test_arbitrary_object_defaults_to_user(self):
        assert get_user_role(object()) == "user"


class TestIsAdmin:
    def test_admin_role(self):
        user = {"user_id": "abc", "role": "admin"}
        assert is_admin(user) is True

    def test_non_admin_role(self):
        user = {"user_id": "abc", "role": "user"}
        assert is_admin(user) is False

    def test_userlike_admin(self):
        user = MagicMock()
        user.id = "obj-id"
        user.email = "obj@example.com"
        user.role = "admin"
        assert is_admin(user) is True

    def test_userlike_non_admin(self):
        user = MagicMock()
        user.id = "obj-id"
        user.email = "obj@example.com"
        user.role = "member"
        assert is_admin(user) is False


class TestVerifyOwnership:
    def test_none_resource_user_id_raises_404(self):
        user = {"user_id": "abc", "role": "user"}
        with pytest.raises(HTTPException) as exc:
            verify_ownership(None, user, resource_name="memory")
        assert exc.value.status_code == 404
        assert "Memory not found" in exc.value.detail

    def test_matching_owner(self):
        user = {"user_id": "abc", "role": "user"}
        # Should not raise
        verify_ownership("abc", user)

    def test_mismatching_owner_raises_404(self):
        user = {"user_id": "abc", "role": "user"}
        with pytest.raises(HTTPException) as exc:
            verify_ownership("def", user, resource_name="context")
        assert exc.value.status_code == 404
        assert "Context not found or not owned by you" in exc.value.detail

    def test_admin_bypass_mismatch(self):
        user = {"user_id": "admin1", "role": "admin"}
        # Admin should bypass ownership check
        verify_ownership("def", user, allow_admin=True)

    def test_admin_bypass_disabled(self):
        user = {"user_id": "admin1", "role": "admin"}
        with pytest.raises(HTTPException) as exc:
            verify_ownership("def", user, allow_admin=False)
        assert exc.value.status_code == 404

    def test_custom_resource_name(self):
        user = {"user_id": "abc", "role": "user"}
        with pytest.raises(HTTPException) as exc:
            verify_ownership(None, user, resource_name="workspace")
        assert "Workspace not found" in exc.value.detail
