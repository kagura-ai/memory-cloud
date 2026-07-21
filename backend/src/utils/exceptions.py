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
    """Authorization failed - insufficient permissions (403).

    ``reason`` is a private classification for structured logging — never
    serialized to clients. By CWE-639 design the response carries a uniform
    ``"Insufficient permissions"`` message; exposing the deny sub-reason
    (workspace_deleted / not_a_member / role_too_low / ...) would
    re-introduce the workspace-enumeration vector that this exception type
    is meant to close (#401 gate2 CSO finding).
    """

    def __init__(
        self,
        message: str = "Insufficient permissions",
        *,
        reason: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, status_code=403, error_code="AUTH-101", **details)
        self.reason = reason


class AdminProtectionError(MemoryCloudException):
    """System-admin invariant blocks this operation (403).

    Distinct from ``AuthorizationError``: the caller IS authorized to
    perform admin actions in general — the request is blocked by a
    per-invariant protection rule (initial-admin sanctity, last-admin
    existence) that exists to prevent the platform from being left
    without any administrator. Surfaces 403 because the caller cannot
    retry their way through it; the block is structural, not credential.

    Mirroring ``AuthorizationError``'s CWE-639 pattern, ``reason`` is a
    private classification (``"initial_admin"`` / ``"last_admin"``) for
    structured-log breadcrumbs only — kept off the response body so
    future ``**details`` kwargs cannot quietly create a leak path. The
    user-facing ``message`` already names the specific protection
    (admin governance is documented platform behavior), so the
    workspace-enumeration concern that motivated AuthorizationError's
    uniform message does not apply here.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message, status_code=403, error_code="ADMIN-001")
        self.reason = reason


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


# RES-004 is reserved for the deprecated /api/v1/attachments/* surface (Issue #555).
# Emitted directly as a JSONResponse from api/routes/attachments.py so the response
# can carry RFC 8594 Sunset/Deprecation/Link headers — which the global
# MemoryCloudException handler does not propagate. No exception subclass is defined
# here because nothing raises it; the route returns the response inline.


class ConflictError(MemoryCloudException):
    """Resource conflict (409)."""

    def __init__(self, message: str = "Resource conflict", **details: Any):
        super().__init__(message, status_code=409, error_code="RES-002", **details)


class ConnectorScopeError(MemoryCloudException):
    """The connector's Slack bot token lacks a scope required for this call (409).

    Issue #1391: ``GET /workspace-connectors/{id}/channels`` proxies Slack's
    ``conversations.list`` to back the settings channel picker. Legacy installs
    (or manual binds of older apps) whose bot token predates the
    ``channels:read`` scope get Slack's ``missing_scope`` error; the endpoint
    maps it to this structured 409 so the picker degrades to manual-ID entry
    instead of surfacing a raw 5xx. ``CONNECTOR-SCOPE`` is the stable code the
    frontend routes on. It is also used when the connector carries no bot token
    at all (never OAuth-installed / token cleared) — the client fallback is the
    same manual-entry lane, so a single signal keeps that contract simple.
    """

    def __init__(
        self,
        message: str = ("The connector's Slack token is missing a required scope (channels:read)."),
    ) -> None:
        super().__init__(message, status_code=409, error_code="CONNECTOR-SCOPE")


class ExportTooLargeError(MemoryCloudException):
    """Context has more memories than a single export can return (413).

    Issue #950: the context-portability export returns one JSON body with no
    pagination. A hard cap (``memory_count`` echoed in ``details``) is the
    zero-cost safety valve that keeps the endpoint's contract stable while a
    streaming / paginated variant — or workspace-wide export — is deferred to
    a follow-up. 413 (not 400/422): the request is well-formed; the *result*
    is too large to serve in this shape.
    """

    def __init__(self, memory_count: int, limit: int) -> None:
        super().__init__(
            f"Context has {memory_count} memories, exceeding the single-export "
            f"limit of {limit}. A paginated / workspace export is a planned follow-up.",
            status_code=413,
            error_code="EXPORT-001",
            memory_count=memory_count,
            limit=limit,
        )


class ValidationError(MemoryCloudException):
    """Validation error (422)."""

    def __init__(self, message: str, field: str | None = None, **details: Any):
        super().__init__(message, status_code=422, error_code="VAL-001", field=field, **details)


class BadRequestError(MemoryCloudException):
    """Request violates a state precondition (400).

    Generic 400 for service-layer state-precondition failures (e.g.
    "user is already an admin", "user is not an admin"). Each call site
    supplies a use-case-specific message and overrides ``error_code`` so
    SDK consumers can route on a stable identifier rather than parsing
    the free-form message.

    Distinct from ``ValidationError`` (422): this is a state mismatch
    against the *current* server-side state, not a shape/format failure
    in the request body. The request is well-formed; the world is not in
    the state the caller assumed.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "REQ-001",
        **details: Any,
    ) -> None:
        super().__init__(message, status_code=400, error_code=error_code, **details)


class WorkerAppNotFoundError(MemoryCloudException):
    """The app-qualified worker selector is unknown (404)."""

    def __init__(self, app_key: str) -> None:
        super().__init__(
            "Worker app identity not found",
            status_code=404,
            error_code="WORKER-APP-001",
            app_key=app_key,
        )


class WorkerAppDisabledError(MemoryCloudException):
    """The app identity is explicitly disabled and must be evicted (410)."""

    def __init__(self, app_key: str) -> None:
        super().__init__(
            "Worker app identity is disabled",
            status_code=410,
            error_code="WORKER-APP-002",
            app_key=app_key,
        )


class WorkerAppNotReadyError(MemoryCloudException):
    """The app identity exists but lacks active verification material (409)."""

    def __init__(self, app_key: str) -> None:
        super().__init__(
            "Worker app identity is not ready",
            status_code=409,
            error_code="WORKER-APP-003",
            app_key=app_key,
        )


class WorkerConnectorNotReadyError(MemoryCloudException):
    """No complete connector config exists for an app-qualified team (404)."""

    def __init__(self) -> None:
        super().__init__(
            "No ready connector for this team",
            status_code=404,
            error_code="WORKER-CONNECTOR-001",
        )


class WorkerAppOperationError(MemoryCloudException):
    """An app-identity lifecycle write failed without exposing secret context."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"Failed to {operation} worker app identity",
            status_code=500,
            error_code="WORKER-APP-004",
        )


class UnsupportedMediaTypeError(MemoryCloudException):
    """Content-Type not in the platform allow-list (415).

    Issue #553: returned by ``FileStorageService.reserve_upload`` when the
    declared ``content_type`` is not in the platform allow-list. The
    comparison normalizes the declared value before lookup: RFC 7231
    parameters are stripped (``text/plain; charset=utf-8`` → ``text/plain``)
    and the bare type/subtype is lowercased (RFC 6838). So this 415 fires
    only after shape validation passes — malformed input (control chars,
    non type/subtype shape) is reported as ``ValidationError`` (422),
    preserving the policy-vs-shape semantic split.

    Distinct from ``ValidationError`` so REST callers receive HTTP 415
    (semantically correct for "I refuse to process this content_type")
    instead of the generic 422 used for shape/format failures, and so MCP
    callers see a dedicated ``unsupported_media_type`` vocab.

    The error_code uses the dedicated ``MEDIA-`` namespace rather than
    ``VAL-`` — this is a capability/policy rejection, not a shape/format
    failure, and SDKs that route on ``error_code`` should not have to
    distinguish 415 from 422 inside the same prefix.

    The ``allowed`` list is included in ``details`` so SDKs and UIs can
    render a precise rejection message without a separate API call.
    """

    def __init__(
        self,
        content_type: str,
        allowed: set[str] | list[str],
        inferred_content_type: str | None = None,
    ) -> None:
        # Issue #961: when ``inferred_content_type`` is supplied, the rejection
        # is an extension/declared-MIME *mismatch* (the declared value passed
        # the allow-list, but the filename extension implies a different — and
        # possibly disallowed — type, e.g. an .svg declared text/plain). Echo
        # both values so the caller sees why. When omitted, behaviour is
        # unchanged: a plain allow-list rejection of the declared type.
        details: dict[str, object] = {
            "content_type": content_type,
            "allowed": sorted(allowed),
        }
        if inferred_content_type is not None:
            message = (
                f"Content type '{content_type}' does not match the filename "
                f"extension (which implies '{inferred_content_type}')"
            )
            details["inferred_content_type"] = inferred_content_type
        else:
            message = f"Content type '{content_type}' not allowed"
        super().__init__(
            message,
            status_code=415,
            error_code="MEDIA-001",
            **details,
        )


# Workspace Slot Bonus Errors (Issue #676 — admin slot bonus UI)


class InsufficientReasonError(MemoryCloudException):
    """Admin tried to shrink workspace_slot_bonus past current owned_count without a reason (400).

    The PATCH /admin/users/{id}/workspace_slot_bonus endpoint requires a non-empty
    ``reason`` only when the operation would create an over-cap state
    (``new_cap < current_owned``). Other deltas (any non-negative delta, or a
    negative delta that still leaves the user at-or-under-cap) accept ``None``.
    The constraint is mirrored in the frontend modal so the admin is informed
    before submit.

    Distinct from ``ValidationError`` (422): the request body is well-formed —
    ``reason: null`` is a valid shape. The 400 fires because the *combination*
    of (delta, current_owned, current_bonus) makes the missing reason a
    business-rule violation.
    """

    def __init__(self) -> None:
        super().__init__(
            "Reason required: this change reduces the cap below the user's current owned count.",
            status_code=400,
            error_code="BONUS-001",
        )


class BonusBelowZeroError(MemoryCloudException):
    """Resulting workspace_slot_bonus would be negative (400).

    Defense-in-depth: the DB ``workspace_slot_bonus_nonneg`` CHECK constraint
    will also reject this at commit time, but doing so surfaces a generic
    IntegrityError to the client instead of a structured 400 with this
    error_code. The app-level guard runs before the UPDATE so SDK consumers
    can route on ``BONUS-002``.
    """

    def __init__(self, *, current: int, delta: int) -> None:
        super().__init__(
            f"Bonus cannot go below zero (current={current}, delta={delta}).",
            status_code=400,
            error_code="BONUS-002",
            current=current,
            delta=delta,
        )


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

    def __init__(
        self,
        message: str = "Quota exceeded",
        quota_type: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            message,
            status_code=429,
            error_code="QUOTA-001",
            quota_type=quota_type,
            **details,
        )


class EmbeddingSpendCapExceeded(QuotaExceededError):
    """BYOK embedding spend cap reached (429).

    Issue #709: raised when a workspace's daily or monthly embedding spend
    (summed from ``LLMCallLog.cost_usd WHERE paid_by='byok'`` via the Redis
    counter) has reached the effective cap for that workspace. Used as the
    pre-call gate in ``EmbeddingService._get_client`` so the actual external
    API call never fires for over-cap requests — protecting workspace owners
    from runaway BYOK costs (the #708 Option A "Embedding Drain Attack"
    threat).

    ``period`` is ``"daily"`` or ``"monthly"`` so the frontend can render the
    right message and link to the right settings panel. Concrete USD figures
    are kept in ``details`` so structured logs / responses can show the
    operator how far over the cap they are without leaking other workspace
    state.
    """

    def __init__(
        self,
        message: str = "Embedding spend cap exceeded",
        *,
        period: str,
        cap_usd: float,
        current_usd: float,
        workspace_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            quota_type=f"embedding_spend_{period}",
            period=period,
            cap_usd=cap_usd,
            current_usd=current_usd,
            workspace_id=workspace_id,
        )
        # QUOTA-002 distinguishes the embedding cap from the generic quota
        # error so client-side error handlers can branch (e.g. show a
        # "raise your cap" CTA instead of "upgrade your plan").
        self.error_code = "QUOTA-002"


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


class EmailDispatchError(ExternalServiceError):
    """Email-provider dispatch failed (503, overrides ExternalServiceError 502).

    Issue #469: raised by ``AccountErasureService.request_self_service_erasure``
    when ``send_erasure_confirmation`` fails for an OAuth user. The 503
    status (instead of the 502 default) signals retriable: the user can
    retry the request once the email backend recovers. The pending row
    has already been rolled back by the time this exception fires, so
    there is no committed request to cancel — the user simply re-issues
    ``POST /me/account/erasure-request``. There is no in-band fallback
    because the response body intentionally withholds the raw
    ``confirm_token`` for OAuth users — email is the canonical delivery
    channel for the OAuth confirm path.

    The constructor is **zero-argument by design**: any string passed
    here would land in ``self.message`` and ``memory_cloud_exception_handler``
    surfaces ``self.message`` directly in both structured logs and the
    JSON response body. SDK error messages and request bodies for the
    confirmation send embed ``confirm_url`` (which contains the raw
    token), so accepting a free-form ``message`` argument creates a
    quiet exfiltration path. Callers MUST log distinguishing metadata
    (``error_type``, ``status_code``) via structlog at the call site
    rather than embedding it in the exception.
    """

    def __init__(self) -> None:
        super().__init__("Email dispatch", message=None, error_code="EXT-205")
        # ExternalServiceError hardcodes 502; override to 503 so FastAPI's
        # global handler maps to a retriable response. Direct attribute
        # mutation avoids reworking the parent's signature.
        self.status_code = 503


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
