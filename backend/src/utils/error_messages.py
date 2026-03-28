"""Standardized error messages for API responses.

Issue #271 Code Review M-1: Centralized error messages for consistency.

Usage:
    from utils.error_messages import ErrorMessages

    raise HTTPException(400, ErrorMessages.NO_ORG_SELECTED)
"""


class ErrorMessages:
    """Standardized error messages for API responses."""

    # Workspace-related
    NO_ORG_SELECTED = "No workspace selected. Please select an workspace first."
    DIFFERENT_ORG = (
        "This resource belongs to a different workspace. Please switch workspaces first."
    )
    NOT_ORG_MEMBER = "You are not a member of this workspace."

    # Context-related
    CONTEXT_NOT_FOUND = "Context not found."
    CONTEXT_DIFFERENT_ORG = (
        "This context belongs to a different workspace. Please switch workspaces first."
    )
    PRIVATE_CONTEXT_ONLY_CREATOR = "This is a private context (creator only)."
    CANNOT_ADD_MEMBERS_TO_PRIVATE = (
        "Cannot add members to a private context. Change to Shared first."
    )

    # Resource Tokens
    RESOURCE_TOKEN_NOT_FOUND = "Resource token not found or not owned by you."
    RESOURCE_NOT_IN_ORG = "Resource ID not found in your workspace or you don't have access to it."
    TOKEN_DIFFERENT_ORG = "This resource token belongs to a different workspace."

    # Plan/Quota
    SHARED_CONTEXTS_REQUIRE_PRO = (
        "Shared contexts require Pro plan. Upgrade your plan to share contexts with your team."
    )
    FEATURE_REQUIRES_PLAN = "This feature requires {plan} plan. Please upgrade."

    # Permissions
    INSUFFICIENT_PERMISSIONS = "You don't have permission to perform this action."
    OWNER_ONLY = "Only the workspace owner can perform this action."
    ADMIN_REQUIRED = "This action requires admin privileges."

    # Validation
    INVALID_INPUT = "Invalid input: {details}"
    RESOURCE_ID_DUPLICATE = "Resource ID is already used by another context in your workspace."
    RESOURCE_ID_IN_USE = (
        "Cannot change to private: This context has a resource_id and is used by Resource Tokens."
    )
