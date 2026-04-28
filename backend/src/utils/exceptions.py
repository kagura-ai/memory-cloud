"""Custom exceptions for Kagura Memory Cloud.

Exception hierarchy with error codes and HTTP status codes for API responses.

Based on: kagura-ai/src/kagura/exceptions.py
"""

from typing import Any


class MemoryCloudException(Exception):
    """Base exception for all Kagura Memory Cloud errors.

    Attributes:
        message: Human-readable error message
        status_code: HTTP status code for API responses
        error_code: Machine-readable error code (e.g., "AUTH-001")
        details: Additional context about the error
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_code: str | None = None,
        **details: Any,
    ) -> None:
        """Initialize exception.

        Args:
            message: Error message
            status_code: HTTP status code
            error_code: Error code (e.g., "AUTH-001")
            **details: Additional context
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code or self.__class__.__name__
        self.details = details


# Authentication & Authorization Errors (4xx)


class AuthenticationError(MemoryCloudException):
    """Authentication failed (401)."""

    def __init__(
        self,
        message: str = "Authentication failed",
        *,
        status_code: int = 401,
        error_code: str = "AUTH-001",
        **details: Any,
    ) -> None:
        super().__init__(message, status_code=status_code, error_code=error_code, **details)


class InvalidCredentialsError(AuthenticationError):
    """Invalid credentials (401)."""

    def __init__(self, message: str = "Invalid credentials"):
        super().__init__(message, error_code="AUTH-002")


class TokenExpiredError(AuthenticationError):
    """Token expired (401)."""

    def __init__(self, message: str = "Token has expired"):
        super().__init__(message, error_code="AUTH-003")


class AuthorizationError(MemoryCloudException):
    """Authorization failed - insufficient permissions (403)."""

    def __init__(self, message: str = "Insufficient permissions", **details: Any):
        super().__init__(message, status_code=403, error_code="AUTH-101", **details)


class APIKeyError(MemoryCloudException):
    """API key related error (401)."""

    def __init__(
        self,
        message: str = "Invalid or missing API key",
        *,
        status_code: int = 401,
        error_code: str = "AUTH-201",
        **details: Any,
    ) -> None:
        super().__init__(message, status_code=status_code, error_code=error_code, **details)


class APIKeyRevokedError(APIKeyError):
    """API key has been revoked (401)."""

    def __init__(self) -> None:
        super().__init__("API key has been revoked", error_code="AUTH-202")


class APIKeyExpiredError(APIKeyError):
    """API key has expired (401)."""

    def __init__(self) -> None:
        super().__init__("API key has expired", error_code="AUTH-203")


# Resource Errors (4xx)


class NotFoundException(MemoryCloudException):
    """Resource not found (404)."""

    def __init__(self, resource: str, resource_id: str | None = None):
        message = f"{resource} not found"
        if resource_id:
            message += f": {resource_id}"
        super().__init__(message, status_code=404, error_code="RES-001")


class MemoryGoneError(MemoryCloudException):
    """Resource existed but was soft-deleted (410 Gone).

    Distinct from NotFoundException so clients can stop retrying instead of
    interpreting 404 as "maybe transient". Used by Issue #439's PATCH path
    when the target memory has ``deleted_at IS NOT NULL``.
    """

    def __init__(self, resource: str, resource_id: str | None = None):
        message = f"{resource} has been deleted"
        if resource_id:
            message += f": {resource_id}"
        super().__init__(message, status_code=410, error_code="RES-003")


class ConflictError(MemoryCloudException):
    """Resource conflict (409)."""

    def __init__(self, message: str = "Resource conflict"):
        super().__init__(message, status_code=409, error_code="RES-002")


class ValidationError(MemoryCloudException):
    """Validation error (422)."""

    def __init__(self, message: str, field: str | None = None, **details: Any):
        super().__init__(message, status_code=422, error_code="VAL-001", field=field, **details)


# Rate Limiting & Quota Errors (429)


class RateLimitError(MemoryCloudException):
    """Rate limit exceeded (429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int | None = None,
    ):
        super().__init__(message, status_code=429, error_code="RATE-001", retry_after=retry_after)

    @property
    def retry_after(self) -> int | None:
        """Seconds the client should wait before retrying, if known."""
        retry = self.details.get("retry_after")
        return int(retry) if retry is not None else None


class QuotaExceededError(MemoryCloudException):
    """Quota exceeded (429)."""

    def __init__(self, message: str = "Quota exceeded", quota_type: str | None = None) -> None:
        super().__init__(message, status_code=429, error_code="QUOTA-001", quota_type=quota_type)


class FeatureNotAvailableError(MemoryCloudException):
    """Feature not available on current plan tier (403)."""

    def __init__(
        self, message: str = "Feature not available on current plan", feature: str | None = None
    ) -> None:
        super().__init__(message, status_code=403, error_code="FEAT-001", feature=feature)


# Database Errors (5xx)


class DatabaseError(MemoryCloudException):
    """Database operation failed (500)."""

    def __init__(
        self,
        message: str = "Database operation failed",
        *,
        status_code: int = 500,
        error_code: str = "DB-001",
        **details: Any,
    ):
        super().__init__(message, status_code=status_code, error_code=error_code, **details)


class DatabaseConnectionError(DatabaseError):
    """Database connection failed (503)."""

    def __init__(self, message: str = "Database connection failed"):
        super().__init__(message, status_code=503, error_code="DB-002")


# External Service Errors (5xx)


class ExternalServiceError(MemoryCloudException):
    """External service error (502)."""

    def __init__(
        self, service: str, message: str | None = None, error_code: str = "EXT-001", **details: Any
    ):
        msg = f"{service} service error"
        if message:
            msg += f": {message}"
        super().__init__(msg, status_code=502, error_code=error_code, **details)


class QdrantError(ExternalServiceError):
    """Qdrant service error (502)."""

    def __init__(self, message: str | None = None):
        super().__init__("Qdrant", message)


class RedisError(ExternalServiceError):
    """Redis service error (502)."""

    def __init__(self, message: str | None = None):
        super().__init__("Redis", message, error_code="EXT-102")


class OpenAIError(ExternalServiceError):
    """OpenAI API error (502)."""

    def __init__(self, message: str | None = None):
        super().__init__("OpenAI", message, error_code="EXT-201")


class CohereError(ExternalServiceError):
    """Cohere API error (502)."""

    def __init__(self, message: str | None = None):
        super().__init__("Cohere", message, error_code="EXT-202")


class VoyageError(ExternalServiceError):
    """Voyage AI API error (502).

    Issue #105: Voyage AI reranker support.
    """

    def __init__(self, message: str | None = None):
        super().__init__("Voyage", message, error_code="EXT-203")


class StripeError(ExternalServiceError):
    """Stripe API error (502).

    Issue #468: raised when stripe-python returns an unexpected shape
    (e.g. a successful Session.create with no ``url`` populated) so the
    failure routes through ``memory_cloud_exception_handler`` instead of
    bubbling as an unhandled 500.
    """

    def __init__(self, message: str | None = None):
        super().__init__("Stripe", message, error_code="EXT-204")


class EmailDeliveryError(ExternalServiceError):
    """Transactional email provider error (502).

    Issue #478: raised when the configured email provider (e.g. Resend)
    fails in a way the caller wants surfaced as a typed 502 rather than
    swallowed as best-effort. Most ``EmailService`` callers do NOT raise
    on send failure — they log + return ``False`` per the Protocol's
    "do not raise" contract — so this is reserved for paths that must
    fail loudly (e.g. fail-fast on misconfiguration at boot).
    """

    def __init__(self, message: str | None = None):
        super().__init__("Email", message, error_code="EXT-205")


# OAuth2 Token Errors (401) - RFC 6750


class TokenRevokedError(AuthenticationError):
    """OAuth2 access token has been revoked."""

    def __init__(self, message: str = "The access token has been revoked"):
        super().__init__(message)
        self.error_code = "invalid_token"
        self.error_description = message


class InvalidTokenError(AuthenticationError):
    """OAuth2 access token is invalid."""

    def __init__(self, message: str = "The access token is invalid"):
        super().__init__(message)
        self.error_code = "invalid_token"
        self.error_description = message


# Internal Errors (5xx)


class ConfigurationError(MemoryCloudException):
    """Configuration error (500)."""

    def __init__(self, message: str):
        super().__init__(message, status_code=500, error_code="CFG-001")


class InternalError(MemoryCloudException):
    """Internal server error (500)."""

    def __init__(self, message: str = "Internal server error", **details: Any):
        super().__init__(message, status_code=500, error_code="INT-001", **details)


# Account Erasure Errors (Issue #360, GDPR Art.17 / APPI compliance)


class ErasureRequestNotFoundError(NotFoundException):
    """No erasure request found (404)."""

    def __init__(self, user_id: str | None = None):
        super().__init__("Erasure request", resource_id=user_id)
        self.error_code = "ERASURE-001"


class ErasureTokenInvalidError(MemoryCloudException):
    """Confirmation token is missing, expired, or does not match (400)."""

    def __init__(self, message: str = "Invalid or expired confirmation token"):
        super().__init__(message, status_code=400, error_code="ERASURE-002")


class ErasureForbiddenError(MemoryCloudException):
    """Erasure cannot proceed for this user (403).

    Raised when the caller does not have permission to erase this account
    (e.g. password mismatch on self-service path) or when the operation is
    blocked by business rules other than initial-admin protection.
    """

    def __init__(self, message: str = "Erasure not permitted"):
        super().__init__(message, status_code=403, error_code="ERASURE-003")


class InitialAdminCannotBeErasedError(MemoryCloudException):
    """The initial system administrator cannot be erased (403).

    Mirrors SystemAdminService.can_delete_admin() — the initial admin row
    is permanently protected to prevent the platform from being left
    without any administrator.
    """

    def __init__(self) -> None:
        super().__init__(
            "Cannot erase the initial system administrator. This is a protected account.",
            status_code=403,
            error_code="ERASURE-004",
        )


class WorkspaceTransferRequiredError(MemoryCloudException):
    """User owns a shared workspace and must transfer ownership before erasure (409).

    Raised when the user is the sole owner of a workspace that has other
    members and no alternate admin to auto-transfer ownership to. The user
    must promote another member to admin (or remove members) before retrying.
    """

    def __init__(self, workspace_id: str, member_count: int):
        super().__init__(
            (
                f"Workspace {workspace_id} has {member_count} other member(s) "
                f"and no alternate admin. Transfer ownership before erasure."
            ),
            status_code=409,
            error_code="ERASURE-005",
            workspace_id=workspace_id,
            member_count=member_count,
        )


class ErasureAlreadyInProgressError(MemoryCloudException):
    """An erasure request for this user is already pending or in progress (409)."""

    def __init__(self, status: str):
        super().__init__(
            f"An erasure request is already {status} for this account.",
            status_code=409,
            error_code="ERASURE-006",
            existing_status=status,
        )
