"""``POST /internal/workspaces/{id}/capacity-lock-notice`` (#1941).

TestClient + mocked DB: the real service-token auth, the projection hand-off,
idempotency through the Redis claim, and a response that never carries the
recipient address.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.base import get_db
from services.capacity_lock import CapacityLock

_WS_ID = str(uuid4())
_PATH = f"/internal/workspaces/{_WS_ID}/capacity-lock-notice"
_BODY = {"period_end": "2026-11-01T00:00:00Z"}
_AUTH = {"Authorization": "Bearer secret"}
_OWNER = "owner@example.test"

_LOCK = CapacityLock(
    memory_count=1200,
    memory_limit=1000,
    over_memories=200,
    used_bytes=0,
    storage_limit_bytes=100,
    over_bytes=0,
    cleanup_url="https://app.example.test/workspace/settings/plan",
)


def _ws():
    ws = MagicMock()
    ws.id = _WS_ID
    ws.name = "Research"
    ws.owner_user_id = "owner-1"
    ws.entitlement_source = "external_billing"
    return ws


class _Redis:
    """A SETNX-faithful stand-in: the second claim on a key fails."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}
        self.deleted: list[str] = []

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True

    async def delete(self, key):
        self.keys.pop(key, None)
        self.deleted.append(key)


@pytest.fixture
def harness(monkeypatch):
    settings = MagicMock(billing_service_token="secret", frontend_url="https://app.example.test")
    monkeypatch.setattr("api.routes.internal_billing.get_settings", lambda: settings)
    state = MagicMock()
    state.settings = settings
    state.redis = _Redis()
    state.email = MagicMock(send_capacity_lock_notice=AsyncMock(return_value=True))
    state.lock = _LOCK
    state.workspace = _ws()
    state.owner_email = _OWNER

    async def mock_db():
        db = MagicMock()
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = state.workspace
        email_result = MagicMock()
        email_result.scalar_one_or_none.return_value = state.owner_email
        db.execute = AsyncMock(side_effect=[ws_result, email_result])
        yield db

    app.dependency_overrides[get_db] = mock_db
    with (
        patch(
            "services.capacity_lock.projected_free_capacity",
            AsyncMock(side_effect=lambda db, ws: state.lock),
        ),
        patch("db.redis.get_redis_client", lambda: state.redis),
        patch("services.email_service.get_email_service", lambda: state.email),
    ):
        state.client = TestClient(app, raise_server_exceptions=False)
        yield state
    app.dependency_overrides.clear()


class TestAuth:
    def test_unset_token_is_503(self, harness) -> None:
        harness.settings.billing_service_token = ""
        assert harness.client.post(_PATH, json=_BODY, headers=_AUTH).status_code == 503

    def test_missing_token_is_401(self, harness) -> None:
        assert harness.client.post(_PATH, json=_BODY).status_code == 401

    def test_wrong_token_is_401(self, harness) -> None:
        resp = harness.client.post(_PATH, json=_BODY, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401


class TestTheNotice:
    def test_over_capacity_sends_once_to_the_owner(self, harness) -> None:
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"sent": True, "reason": None, "over_memories": 200, "over_bytes": 0}
        assert _OWNER not in resp.text
        kwargs = harness.email.send_capacity_lock_notice.await_args.kwargs
        assert kwargs["to_email"] == _OWNER
        assert kwargs["workspace_name"] == "Research"
        assert kwargs["period_end"] == datetime(2026, 11, 1, tzinfo=UTC)
        assert kwargs["over_memories"] == 200
        assert kwargs["cleanup_url"].endswith("/workspace/settings/plan")
        assert kwargs["contexts_url"] == "https://app.example.test/workspace/contexts"

    def test_a_repeat_for_the_same_period_is_already_sent(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json()["sent"] is False
        assert resp.json()["reason"] == "already_sent"
        assert harness.email.send_capacity_lock_notice.await_count == 1

    def test_the_same_instant_in_another_offset_is_the_same_period(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        resp = harness.client.post(
            _PATH, json={"period_end": "2026-11-01T09:00:00+09:00"}, headers=_AUTH
        )
        assert resp.json()["reason"] == "already_sent"

    def test_a_new_period_within_24h_is_cooldown(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        resp = harness.client.post(
            _PATH, json={"period_end": "2026-12-01T00:00:00Z"}, headers=_AUTH
        )
        assert resp.json()["sent"] is False
        assert resp.json()["reason"] == "cooldown"
        assert harness.email.send_capacity_lock_notice.await_count == 1
        # The refused period's claim was released: it can be sent later.
        assert not any(k.endswith("2026-12-01T00:00:00") for k in harness.redis.keys)

    def test_a_new_period_sends_again_after_the_cooldown(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        harness.redis.keys.pop(f"capacity_lock_notice_cooldown:{_WS_ID}")  # 24h passed
        resp = harness.client.post(
            _PATH, json={"period_end": "2026-12-01T00:00:00Z"}, headers=_AUTH
        )
        assert resp.json()["sent"] is True

    def test_the_cooldown_is_a_day_per_workspace(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert harness.redis.keys[f"capacity_lock_notice_cooldown:{_WS_ID}"] == 24 * 3600

    def test_the_claim_outlives_the_period_end(self, harness) -> None:
        harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        (ttl,) = [v for k, v in harness.redis.keys.items() if "cooldown" not in k]
        assert ttl >= 7 * 24 * 3600

    def test_within_capacity_sends_nothing(self, harness) -> None:
        harness.lock = None
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json() == {
            "sent": False,
            "reason": "within_capacity",
            "over_memories": 0,
            "over_bytes": 0,
        }
        harness.email.send_capacity_lock_notice.assert_not_awaited()
        assert harness.redis.keys == {}

    def test_a_failed_delivery_releases_the_claim(self, harness) -> None:
        harness.email.send_capacity_lock_notice = AsyncMock(return_value=False)
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json()["reason"] == "delivery_failed"
        assert harness.redis.keys == {}
        harness.email.send_capacity_lock_notice = AsyncMock(return_value=True)
        assert harness.client.post(_PATH, json=_BODY, headers=_AUTH).json()["sent"] is True

    def test_no_owner_email(self, harness) -> None:
        harness.owner_email = None
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json()["reason"] == "no_owner_email"
        harness.email.send_capacity_lock_notice.assert_not_awaited()

    def test_a_missing_or_deleted_workspace_is_404(self, harness) -> None:
        harness.workspace = None
        assert harness.client.post(_PATH, json=_BODY, headers=_AUTH).status_code == 404

    def test_an_unavailable_dedup_store_is_503_and_sends_nothing(self, harness) -> None:
        harness.redis.set = AsyncMock(side_effect=ConnectionError("down"))
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.status_code == 503
        harness.email.send_capacity_lock_notice.assert_not_awaited()

    def test_period_end_is_required(self, harness) -> None:
        assert harness.client.post(_PATH, json={}, headers=_AUTH).status_code == 422


class TestAtMostOnce:
    def test_an_uncertain_delivery_counts_as_sent_and_keeps_the_claim(self, harness) -> None:
        harness.email.send_capacity_lock_notice = AsyncMock(side_effect=TimeoutError())
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json()["sent"] is True
        assert resp.json()["reason"] == "delivery_uncertain"
        assert len(harness.redis.keys) == 2  # period claim and cooldown both kept
        again = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert again.json()["reason"] == "already_sent"


class TestProvenance:
    def test_an_admin_managed_workspace_is_never_warned(self, harness) -> None:
        harness.workspace.entitlement_source = "admin_grant"
        resp = harness.client.post(_PATH, json=_BODY, headers=_AUTH)
        assert resp.json() == {
            "sent": False,
            "reason": "not_billing_managed",
            "over_memories": 0,
            "over_bytes": 0,
        }
        harness.email.send_capacity_lock_notice.assert_not_awaited()
        assert harness.redis.keys == {}
