"""Utility modules for Kagura Memory Cloud.

This package provides common utilities used across the application:
- auth_helpers: User ID extraction, ownership verification
- db_helpers: Transaction management, error handling
- encryption: API key encryption/decryption
- masking: Sensitive value masking for display
- logger: Structured logging
- exceptions: Custom exceptions
"""

from utils.auth_helpers import (
    AuthenticatedUser,
    UserLike,
    get_user_email,
    get_user_id,
    get_user_role,
    is_admin,
    verify_ownership,
)
from utils.db_helpers import db_transaction, execute_with_rollback, with_db_transaction
from utils.masking import mask_email, mask_prefix_only, mask_secret

__all__ = [
    # Auth helpers
    "AuthenticatedUser",
    "UserLike",
    "get_user_id",
    "get_user_email",
    "get_user_role",
    "is_admin",
    "verify_ownership",
    # DB helpers
    "db_transaction",
    "with_db_transaction",
    "execute_with_rollback",
    # Masking
    "mask_secret",
    "mask_prefix_only",
    "mask_email",
]
