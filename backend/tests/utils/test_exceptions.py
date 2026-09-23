"""Tests for custom exceptions."""

import pytest

from config.constants import GATE_ALLOWLIST, GATE_DEPLOYMENT, GATE_PLAN, GATE_QUOTA
from config.plan_tiers import feature_denied_message, get_plan_tier
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
    EmbeddingSpendCapExceeded,
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
        exc = BadRequestError("User is already a system admin", error_code="REQ-101")
        assert exc.status_code == 400
        assert exc.error_code == "REQ-101"

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

    def test_quota_exceeded_stamps_the_quota_gate(self):
        """#1644: every cap is machine-readable as a quota, not just a 429."""
        exc = QuotaExceededError("Context limit reached.", "contexts")
        assert exc.details["gate"] == GATE_QUOTA
        assert exc.details["quota_type"] == "contexts"

    def test_untyped_quota_exceeded_carries_no_gate(self):
        """#1644 review: the type is also raised for the 1 MB memory-size guard
        and the "workspace not found" anomalies. Those are not plan quotas, so
        a ``gate`` would make a client offer an upgrade no tier provides."""
        exc = QuotaExceededError("Memory size 1,000,001 bytes exceeds limit 1,000,000 bytes (1MB).")
        assert exc.error_code == "QUOTA-001"
        assert exc.status_code == 429
        assert "gate" not in exc.details

    def test_untyped_quota_exceeded_drops_a_caller_supplied_gate(self):
        """The type decides, not the caller: an untyped raise cannot be
        mislabelled by passing ``gate`` by hand."""
        exc = QuotaExceededError("Workspace x not found", gate=GATE_QUOTA)
        assert "gate" not in exc.details

    def test_quota_type_outside_the_frozen_vocabulary_carries_no_gate(self):
        """A client maps ``quota_type`` onto a gate key; one it has no key for
        would be an unrenderable gate, so it is not stamped as one."""
        exc = QuotaExceededError("x", "not_a_frozen_type", current=1, limit=1)
        assert "gate" not in exc.details
        assert exc.details["quota_type"] == "not_a_frozen_type"

    def test_quota_exceeded_keeps_a_caller_supplied_gate(self):
        """``quota_gate_details`` already carries ``gate``; splatting it must not
        raise a duplicate-kwarg TypeError."""
        exc = QuotaExceededError("x", "contexts", gate=GATE_QUOTA, current=1, limit=1)
        assert exc.details["gate"] == GATE_QUOTA
        assert (exc.details["current"], exc.details["limit"]) == (1, 1)

    def test_quota_exceeded_accepts_a_403_status_for_the_two_legacy_cap_sites(self):
        """#1644 S5: resource tokens and connector seats have always answered
        403; the details block must not move their status."""
        exc = QuotaExceededError("Token limit reached.", "resource_tokens", status_code=403)
        assert exc.status_code == 403
        assert exc.error_code == "QUOTA-001"
        assert exc.details["gate"] == GATE_QUOTA

    def test_embedding_spend_cap_keeps_quota_002_and_gains_the_gate(self):
        """#1644 J-6: the cap is a quota, but its numbers are USD floats, so it
        carries no ``current`` / ``limit`` counts."""
        exc = EmbeddingSpendCapExceeded(period="daily", cap_usd=0.5, current_usd=0.9)
        assert exc.error_code == "QUOTA-002"
        assert exc.status_code == 429
        assert exc.details["gate"] == GATE_QUOTA
        assert exc.details["quota_type"] == "embedding_spend_daily"
        assert "current" not in exc.details
        assert "limit" not in exc.details

    def test_feature_not_available(self):
        exc = FeatureNotAvailableError(feature="reranking")
        assert exc.status_code == 403
        assert exc.details["feature"] == "reranking"

    def test_feature_not_available_for_feature(self):
        """#1561: one constructor builds every registry-derived feature refusal."""
        exc = FeatureNotAvailableError.for_feature("pro", "connectors")
        assert isinstance(exc, FeatureNotAvailableError)
        assert exc.status_code == 403
        assert exc.error_code == "FEAT-001"
        assert exc.details["feature"] == "connectors"
        assert exc.message == feature_denied_message("pro", "connectors")
        assert "Pro plan" not in exc.message

    def test_feature_not_available_for_feature_none_plan_reads_as_free(self):
        exc = FeatureNotAvailableError.for_feature(None, "resources")
        assert exc.message == feature_denied_message(None, "resources")
        # #1583: the current plan reads as its display name, like the required one.
        assert f"on {get_plan_tier('free').display_name} plan" in exc.message
        assert exc.details["feature"] == "resources"

    def test_feature_not_available_defaults_to_the_plan_gate(self):
        """#1644: the five pre-existing raisers keep meaning "your tier lacks it"."""
        exc = FeatureNotAvailableError(feature="reranking")
        assert exc.details["gate"] == GATE_PLAN

    def test_feature_not_available_for_feature_carries_both_halves_of_the_tier(self):
        """#1644: the KEY a client decides with and the LABEL a CLI renders."""
        exc = FeatureNotAvailableError.for_feature("basic", "team_invitations")
        assert exc.details["gate"] == GATE_PLAN
        assert exc.details["feature"] == "team_invitations"
        assert exc.details["current_plan"] == "basic"
        required = exc.details["required_plan"]
        assert required is not None
        assert exc.details["required_plan_display"] == get_plan_tier(required).display_name

    def test_feature_not_available_for_rollout_is_plan_neutral(self):
        """#1644 S10: an allowlist refusal must never read as an upsell."""
        exc = FeatureNotAvailableError.for_rollout(
            "Memory analysis is not yet enabled for this workspace.", "memory_analysis"
        )
        assert exc.status_code == 403
        assert exc.error_code == "FEAT-001"
        assert exc.details["gate"] == GATE_ALLOWLIST
        assert exc.details["feature"] == "memory_analysis"
        assert "required_plan" not in exc.details
        assert "required_plan_display" not in exc.details

    def test_feature_not_available_for_deployment_is_plan_neutral(self):
        exc = FeatureNotAvailableError.for_deployment(
            "Managed analysis is not configured on this deployment.", "managed_llm"
        )
        assert exc.status_code == 403
        assert exc.details["gate"] == GATE_DEPLOYMENT
        assert exc.details["feature"] == "managed_llm"
        assert "required_plan" not in exc.details

    def test_feature_not_available_gates_are_the_frozen_vocabulary(self):
        from config.constants import GATE_KINDS

        for exc in (
            FeatureNotAvailableError(feature="reranking"),
            FeatureNotAvailableError.for_feature("free", "connectors"),
            FeatureNotAvailableError.for_rollout("not yet", "memory_analysis"),
            FeatureNotAvailableError.for_deployment("off here", "managed_llm"),
        ):
            assert exc.details["gate"] in GATE_KINDS


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
