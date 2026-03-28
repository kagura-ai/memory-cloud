"""Value masking utilities.

Provides common utilities for masking sensitive values like API keys,
tokens, and other secrets for display purposes.

Issue #106: Consolidate redundant code patterns
"""

from __future__ import annotations


def mask_secret(
    value: str,
    show_start: int = 4,
    show_end: int = 4,
    mask_char: str = "*",
    min_mask_length: int = 4,
) -> str:
    """Mask a secret value for safe display.

    Shows first and last N characters, masks the middle.

    Args:
        value: Secret value to mask
        show_start: Number of characters to show at start
        show_end: Number of characters to show at end
        mask_char: Character to use for masking
        min_mask_length: Minimum number of mask characters

    Returns:
        Masked value (e.g., "sk-p****def4")

    Example:
        >>> mask_secret("sk-proj-abc123def456")
        'sk-p************f456'

        >>> mask_secret("short")
        '*****'

        >>> mask_secret("abc123", show_start=2, show_end=2)
        'ab**23'
    """
    if not value:
        return ""

    total_shown = show_start + show_end

    # If value is too short, mask entirely (preserve actual length)
    if len(value) <= total_shown:
        return mask_char * len(value)

    # Calculate mask length
    mask_length = max(min_mask_length, len(value) - total_shown)

    return f"{value[:show_start]}{mask_char * mask_length}{value[-show_end:]}"


def mask_prefix_only(
    value: str,
    show_chars: int = 8,
    mask_suffix: str = "***",
) -> str:
    """Mask a value showing only prefix.

    Shows first N characters, appends mask suffix.

    Args:
        value: Value to mask
        show_chars: Number of characters to show at start
        mask_suffix: Suffix to append after visible characters

    Returns:
        Masked value (e.g., "sk-proj-***")

    Example:
        >>> mask_prefix_only("sk-proj-abc123def456")
        'sk-proj-***'

        >>> mask_prefix_only("short", show_chars=10)
        '***'
    """
    if not value:
        return ""

    if len(value) <= show_chars:
        return mask_suffix

    return f"{value[:show_chars]}{mask_suffix}"


def mask_email(email: str) -> str:
    """Mask an email address for safe display.

    Shows first 2 chars of local part, domain is visible.

    Args:
        email: Email address to mask

    Returns:
        Masked email (e.g., "us***@example.com")

    Example:
        >>> mask_email("user@example.com")
        'us***@example.com'
    """
    if not email or email.count("@") != 1:
        return "***"

    local, domain = email.rsplit("@", 1)

    if len(local) <= 2:
        masked_local = local[0] + "***" if local else "***"
    else:
        masked_local = local[:2] + "***"

    return f"{masked_local}@{domain}"
