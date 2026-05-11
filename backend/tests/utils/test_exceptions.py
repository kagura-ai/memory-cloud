"""Tests for custom exceptions."""

import pytest

from utils.exceptions import (
    AdminProtectionError,
    APIKeyError,
    APIKeyExpiredError,
    APIKeyRevokedError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    CohereError,
    ConfigurationError,
    ConflictError,
    DatabaseConnectionError,
    DatabaseError,
    ExternalServiceError,
    FeatureNotAvailableError,
    InternalError,
    InvalidCredentialsError,
    InvalidTokenError,
    MemoryCloudException,
    NotFoundException,
    OpenAIError,
    QdrantError,
    QuotaExceededError,
    RateLimitError,
    RedisError,
    TokenExpiredError,
    TokenRevokedError,
    ValidationError,
    VoyageError,
)


class TestBaseException:
    """Test MemoryCloudException base class."""

    def test_default_values(self):
        """Test default status_code and error_code."""
        exc = MemoryCloudException("test")
        assert exc.message == "test"
        assert exc.status_code == 500
        assert exc.error_code == "MemoryCloudException"

    def test_custom_values(self):
        """Test custom status_code and error_code."""
        exc = MemoryCloudException("test", status_code=418, error_code="TEAPOT")
        assert exc.status_code == 418
        assert exc.error_code == "TEAPOT"

    def test_details(self):
        """Test extra details are stored."""
        exc = MemoryCloudException("test", field="name")
        assert exc.details["field"] == "name"


class TestAuthErrors:
    """Test authentication/authorization errors."""

    def test_authentication_error(self):
        exc = AuthenticationError("Invalid token")
        assert exc.status_code == 401
        assert exc.error_code == "AUTH-001"

    def test_invalid_credentials(self):
        exc = InvalidCredentialsError()
        assert exc.status_code == 401
        assert exc.error_code == "AUTH-002"
        assert exc.message == "Invalid credentials"

    def test_token_expired(self):
        exc = TokenExpiredError()
        assert exc.status_code == 401
        assert exc.error_code == "AUTH-003"
        assert exc.message == "Token has expired"

    def test_authorization_error(self):
        exc = AuthorizationError("Access denied")
        assert exc.status_code == 403

    def test_authorization_error_reason_is_private(self):
        """CWE-639: ``reason`` lives on ``exc.reason`` (private), not in
        ``exc.details`` — the global handler serializes ``details`` to the
        response body, so leaking ``reason`` would re-introduce the
        workspace-enumeration vector that the uniform "Insufficient
        permissions" message is designed to close. See #401 gate2 CSO.
        """
        exc = AuthorizationError("Insufficient permissions", reason="workspace_deleted")
        assert exc.reason == "workspace_deleted"
        assert "reason" not in exc.details
        assert exc.details == {}

    def test_authorization_error_default_reason_none(self):
        """``reason`` defaults to ``None`` when not provided, so existing
        AuthorizationError raises without a reason kwarg are unaffected."""
        exc = AuthorizationError("Insufficient permissions")
        assert exc.reason is None
        assert exc.details == {}

    def test_admin_protection_error_defaults(self):
        exc = AdminProtectionError("Cannot demote the initial system administrator.")
        assert exc.status_code == 403
        assert exc.error_code == "ADMIN-001"
        assert exc.reason is None
        assert exc.details == {}

    def test_admin_protection_error_carries_reason_privately(self):
        """Mirrors AuthorizationError: ``reason`` lives on ``exc.reason``
        (private), never in ``exc.details``. The handler additionally
        strips ``details`` for AdminProtectionError so any future
        ``**details`` smuggling cannot leak into the response body."""
        exc = AdminProtectionError(
            "Cannot demote the initial system administrator.",
            reason="initial_admin",
        )
        assert exc.reason == "initial_admin"
        assert "reason" not in exc.details
        assert exc.details == {}

    def test_admin_protection_error_rejects_unknown_kwargs(self):
        """Constructor signature is keyword-only on ``reason`` with no
        ``**details`` passthrough — a contributor adding a forensics kwarg
        like ``user_email="..."`` gets a TypeError at construction time
        instead of silently leaking into the response body."""
        with pytest.raises(TypeError):
            AdminProtectionError(  # type: ignore[call-arg]
                "msg",
                user_email="leak@example.com",
            )

    def test_api_key_error(self):
        exc = APIKeyError()
        assert exc.status_code == 401

    def test_api_key_revoked(self):
        exc = APIKeyRevokedError()
        assert exc.status_code == 401
        assert exc.error_code == "AUTH-202"
        assert exc.message == "API key has been revoked"

    def test_api_key_expired(self):
        exc = APIKeyExpiredError()
        assert exc.status_code == 401
        assert exc.error_code == "AUTH-203"
        assert exc.message == "API key has expired"

    def test_token_revoked(self):
        exc = TokenRevokedError()
        assert exc.status_code == 401
        assert exc.error_code == "invalid_token"

    def test_invalid_token(self):
        exc = InvalidTokenError()
        assert exc.status_code == 401


class TestResourceErrors:
    """Test resource-related errors."""

    def test_not_found(self):
        exc = NotFoundException("Memory")
        assert exc.status_code == 404
        assert "Memory not found" in str(exc)

    def test_not_found_with_id(self):
        exc = NotFoundException("Memory", "abc-123")
        assert "abc-123" in str(exc)

    def test_conflict_error(self):
        exc = ConflictError()
        assert exc.status_code == 409

    def test_validation_error(self):
        exc = ValidationError("Bad input", field="email")
        assert exc.status_code == 422
        assert exc.details["field"] == "email"

    def test_bad_request_error_default_code(self):
        exc = BadRequestError("User is already a system admin")
        assert exc.status_code == 400
        assert exc.error_code == "REQ-001"
        assert exc.message == "User is already a system admin"

    def test_bad_request_error_custom_code(self):
        """Call sites override ``error_code`` so SDKs can route on a stable
        identifier without parsing the free-form message."""
        exc = BadRequestError("User is already a system admin", error_code="ADMIN-101")
        assert exc.status_code == 400
        assert exc.error_code == "ADMIN-101"

    def test_bad_request_error_details_passthrough(self):
        """Unlike AdminProtectionError, BadRequestError forwards ``**details``
        because 400 state-precondition errors do not carry the CWE-639
        enumeration risk that motivated the deny-class strip."""
        exc = BadRequestError("bad state", error_code="X-001", field="role")
        assert exc.details == {"field": "role"}


class TestRateLimitErrors:
    """Test rate limit and quota errors."""

    def test_rate_limit(self):
        exc = RateLimitError()
        assert exc.status_code == 429

    def test_rate_limit_with_retry(self):
        exc = RateLimitError(retry_after=60)
        assert exc.details["retry_after"] == 60

    def test_quota_exceeded(self):
        exc = QuotaExceededError()
        assert exc.status_code == 429

    def test_feature_not_available(self):
        exc = FeatureNotAvailableError(feature="reranking")
        assert exc.status_code == 403
        assert exc.details["feature"] == "reranking"


class TestDatabaseErrors:
    """Test database errors."""

    def test_database_error(self):
        exc = DatabaseError()
        assert exc.status_code == 500

    def test_database_connection_error(self):
        exc = DatabaseConnectionError()
        assert exc.status_code == 503
        assert exc.error_code == "DB-002"
        assert exc.message == "Database connection failed"


class TestExternalServiceErrors:
    """Test external service errors."""

    def test_external_service(self):
        exc = ExternalServiceError("TestService", "timed out")
        assert exc.status_code == 502
        assert "TestService" in str(exc)

    def test_qdrant_error(self):
        exc = QdrantError("connection refused")
        assert exc.status_code == 502
        assert "Qdrant" in str(exc)

    def test_redis_error(self):
        exc = RedisError("timeout")
        assert exc.status_code == 502

    def test_openai_error(self):
        exc = OpenAIError("rate limited")
        assert exc.status_code == 502
        assert "OpenAI" in str(exc)

    def test_cohere_error(self):
        exc = CohereError("bad request")
        assert exc.status_code == 502

    def test_voyage_error(self):
        exc = VoyageError("unauthorized")
        assert exc.status_code == 502


class TestConfigAndInternalErrors:
    """Test configuration and internal errors."""

    def test_configuration_error(self):
        exc = ConfigurationError("Missing API key")
        assert exc.status_code == 500

    def test_internal_error(self):
        exc = InternalError()
        assert exc.status_code == 500

    def test_exception_inherits(self):
        """All custom exceptions inherit from MemoryCloudException."""
        exc = NotFoundException("test")
        assert isinstance(exc, MemoryCloudException)
        assert isinstance(exc, Exception)
