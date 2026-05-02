"""Tests for utils/error_messages.py.

Pure constants — a single test mechanically covers all lines.
"""

from utils.error_messages import ErrorMessages


def test_error_messages_exist_and_are_non_empty():
    """All public error message constants must be non-empty strings."""
    constants = [
        ErrorMessages.NO_ORG_SELECTED,
        ErrorMessages.DIFFERENT_ORG,
        ErrorMessages.NOT_ORG_MEMBER,
        ErrorMessages.CONTEXT_NOT_FOUND,
        ErrorMessages.CONTEXT_DIFFERENT_ORG,
        ErrorMessages.PRIVATE_CONTEXT_ONLY_CREATOR,
        ErrorMessages.CANNOT_ADD_MEMBERS_TO_PRIVATE,
        ErrorMessages.RESOURCE_TOKEN_NOT_FOUND,
        ErrorMessages.RESOURCE_NOT_IN_ORG,
        ErrorMessages.TOKEN_DIFFERENT_ORG,
        ErrorMessages.SHARED_CONTEXTS_REQUIRE_PRO,
        ErrorMessages.FEATURE_REQUIRES_PLAN,
        ErrorMessages.INSUFFICIENT_PERMISSIONS,
        ErrorMessages.OWNER_ONLY,
        ErrorMessages.ADMIN_REQUIRED,
        ErrorMessages.INVALID_INPUT,
        ErrorMessages.RESOURCE_ID_DUPLICATE,
        ErrorMessages.RESOURCE_ID_IN_USE,
    ]
    for msg in constants:
        assert isinstance(msg, str)
        assert len(msg) > 0
