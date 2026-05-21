"""Tests for /api/v1/oauth/device/audit-unauth (Issue #779).

Validates the fire-and-forget audit endpoint that logs unauthenticated
/device page hits for monitoring (device-code spraying, bot traffic).

Contract:
- No auth required
- Pydantic max_length=4 on user_code_prefix (full code is auth material)
- Per-IP rate limit (30/min) — silent drop above threshold, still returns 204
- Logs structlog event `device_unauth_hit` with prefix, ip, user_agent
"""

import sys
from pathlib import Path
from unittest.mock import patch

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402


class TestDeviceUnauthAuditEndpoint:
    def test_returns_204_with_valid_prefix(self):
        with patch("api.routes.oauth.increment_counter", return_value=1):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": "ABCD"},
            )
        assert resp.status_code == 204
        assert resp.content == b""

    def test_returns_204_with_empty_prefix(self):
        with patch("api.routes.oauth.increment_counter", return_value=1):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": ""},
            )
        assert resp.status_code == 204

    def test_rejects_prefix_longer_than_4_chars(self):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/oauth/device/audit-unauth",
            json={"user_code_prefix": "ABCDE"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert any("too_long" in (err.get("type", "") or "") for err in body.get("detail", []))

    def test_rejects_prefix_with_disallowed_characters(self):
        # user_code is uppercase alphanumeric (RFC 8628). Anything outside
        # [A-Z0-9] is either malformed input or a log-injection attempt; reject
        # at the schema boundary rather than writing it to structlog.
        client = TestClient(app)
        for bad_prefix in ("abcd", "AB<C", "a&b", "AB\n", " ABC"):
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": bad_prefix},
            )
            assert resp.status_code == 422, (
                f"expected 422 for prefix={bad_prefix!r}, got {resp.status_code}"
            )

    def test_silently_drops_above_rate_limit_but_still_returns_204(self):
        # 31st request from same IP — should NOT 429; should silently drop the log
        # and still return 204 so the client never learns about rate-limit state.
        with patch("api.routes.oauth.increment_counter", return_value=31):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": "ABCD"},
            )
        assert resp.status_code == 204

    def test_logs_device_unauth_hit_with_prefix_ip_and_user_agent(self):
        with (
            patch("api.routes.oauth.increment_counter", return_value=1),
            patch("api.routes.oauth.logger") as mock_logger,
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": "ABCD"},
                headers={"User-Agent": "TestAgent/1.0"},
            )
        assert resp.status_code == 204
        assert mock_logger.info.called, "expected logger.info to be invoked"
        call = mock_logger.info.call_args
        assert call.args[0] == "device_unauth_hit"
        assert call.kwargs.get("user_code_prefix") == "ABCD"
        assert call.kwargs.get("user_agent") == "TestAgent/1.0"
        assert "ip" in call.kwargs
        # UTC timestamp contract (docstring): structlog's TimeStamper is configured
        # with utc=False project-wide, so the route emits an explicit ISO-8601 Z-suffixed field.
        ts = call.kwargs.get("timestamp_utc")
        assert isinstance(ts, str) and ts.endswith("Z"), (
            f"expected timestamp_utc to be an ISO-8601 string with Z suffix, got: {ts!r}"
        )

    def test_rate_limit_path_logs_warning_not_info(self):
        with (
            patch("api.routes.oauth.increment_counter", return_value=31),
            patch("api.routes.oauth.logger") as mock_logger,
        ):
            client = TestClient(app)
            client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": "ABCD"},
            )
        assert mock_logger.warning.called
        assert mock_logger.warning.call_args.args[0] == "device_unauth_audit_rate_limited"
        assert not mock_logger.info.called, (
            "rate-limit branch must NOT also emit the device_unauth_hit info log"
        )

    def test_redis_failure_returns_204_with_warning_log(self):
        # Redis outage during rate-limit check must NOT 500 — fire-and-forget
        # contract holds even when observability is degraded.
        with (
            patch(
                "api.routes.oauth.increment_counter",
                side_effect=RuntimeError("redis down"),
            ),
            patch("api.routes.oauth.logger") as mock_logger,
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/audit-unauth",
                json={"user_code_prefix": "ABCD"},
            )
        assert resp.status_code == 204
        # Warning logged for observability
        assert mock_logger.warning.called
        assert mock_logger.warning.call_args.args[0] == "device_unauth_audit_redis_failure"
        # The info log must NOT fire when Redis is unavailable
        assert not mock_logger.info.called, "redis-failure branch must not emit device_unauth_hit"
