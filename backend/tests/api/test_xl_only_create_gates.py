"""REST "may create" gates for the XL-only re-map (#1551).

Resources, connectors and public features are XL-only to *create*; objects
that already exist on M/L keep working. Each class below pins one route:
creation refused on free / basic / pro with a registry-derived tier name,
allowed on promax, and the existing-object path untouched. Mock-based (no
DB), mirroring the neighbouring route tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db
from utils.exceptions import FeatureNotAvailableError

_WS = uuid.uuid4()
_NON_XL = ["free", "basic", "pro"]


def _result(*, one=None, scalar=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalar.return_value = scalar
    return result


def _token(**overrides) -> SimpleNamespace:
    base = {
        "id": 7,
        "resource_id": "products",
        "resource_pk": uuid.uuid4(),
        "description": None,
        "quota_events_per_hour": 1000,
        "created_by": "owner-1",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_used_at": None,
        "is_active": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# POST /resource-tokens — create
# ---------------------------------------------------------------------------


class TestResourceTokenCreate:
    async def _create(self, plan_name: str):
        from api.routes.resource_tokens import ResourceTokenCreate, create_resource_token

        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        # 1) context exists in workspace → id, 2) plan_name, 3) active count
        db.execute = AsyncMock(
            side_effect=[_result(one=uuid.uuid4()), _result(one=plan_name), _result(scalar=0)]
        )
        manager = MagicMock()
        manager.create_token = AsyncMock(return_value=("kagura_resource_plain", _token()))

        with patch(
            "api.routes.resource_tokens.resolve_resource_pk",
            new=AsyncMock(return_value=uuid.uuid4()),
        ):
            response = await create_resource_token(
                ResourceTokenCreate(resource_id="products", quota_events_per_hour=1000),
                ("owner-1", _WS),
                manager,
                db,
            )
        return response, manager, db

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_name", _NON_XL)
    async def test_refused_below_xl(self, plan_name: str) -> None:
        with pytest.raises(FeatureNotAvailableError) as exc_info:
            await self._create(plan_name)

        assert exc_info.value.status_code == 403
        assert exc_info.value.details["feature"] == "resources"
        assert "XL" in exc_info.value.message
        assert "PRO plan" not in exc_info.value.message

    @pytest.mark.asyncio
    async def test_allowed_on_xl(self) -> None:
        response, manager, db = await self._create("promax")
        assert response.token == "kagura_resource_plain"
        manager.create_token.assert_awaited_once()
        # The tier's numeric cap stays the second gate: the count query ran.
        assert db.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_missing_plan_row_fails_closed(self) -> None:
        """No plan row → no feature. The old ``if plan_name:`` nesting skipped
        the gate entirely and minted the token."""
        with pytest.raises(FeatureNotAvailableError):
            await self._create(None)  # type: ignore[arg-type]


class TestResourceTokenQuotaUpdateKeepsServing:
    """PATCH /resource-tokens/{id}: the ``max_resource_tokens * 10000`` ceiling
    must keep working for tokens that already exist on M/L (pro = 300 000,
    basic = 30 000) — the cap did not move to 0 with the feature gate."""

    async def _update(self, plan_name: str, *, used_by_others: int, new_quota: int):
        from api.routes.resource_tokens import ResourceTokenUpdate, update_resource_token

        token = _token(quota_events_per_hour=500)
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        # 1) token by id+owner, 2) context in workspace, 3) plan_name, 4) sum(other tokens)
        db.execute = AsyncMock(
            side_effect=[
                _result(one=token),
                _result(one=uuid.uuid4()),
                _result(one=plan_name),
                _result(scalar=used_by_others),
            ]
        )
        response = await update_resource_token(
            token.id,
            ResourceTokenUpdate(quota_events_per_hour=new_quota),
            ("owner-1", _WS),
            db,
        )
        return response, token

    @pytest.mark.asyncio
    async def test_pro_ceiling_is_still_300k(self) -> None:
        response, token = await self._update("pro", used_by_others=290_000, new_quota=10_000)
        assert token.quota_events_per_hour == 10_000
        assert response.quota_events_per_hour == 10_000

    @pytest.mark.asyncio
    async def test_pro_over_ceiling_is_400_not_403(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await self._update("pro", used_by_others=290_001, new_quota=10_000)
        assert exc_info.value.status_code == 400
        assert "300000" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_basic_ceiling_is_still_30k(self) -> None:
        _, token = await self._update("basic", used_by_others=20_000, new_quota=10_000)
        assert token.quota_events_per_hour == 10_000


# ---------------------------------------------------------------------------
# PUT /contexts/{id} — is_public=True ("set_public")
# ---------------------------------------------------------------------------


class TestContextSetPublic:
    def _existing(self, *, is_public: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            name="ctx",
            display_name=None,
            description=None,
            summary=None,
            usage_guide=None,
            is_default=False,
            is_locked=False,
            sleep_mode="skip",
            is_private=False,
            is_public=is_public,
            resource_id="products" if is_public else None,
            created_by="owner-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            workspace_id=_WS,
        )

    async def _put(self, plan_name: str | None, existing: SimpleNamespace, **fields):
        from api.routes.contexts import ContextUpdate, update_context

        db = MagicMock()
        # ``plan_name=None`` models a context whose workspace row is missing.
        db.get = AsyncMock(return_value=SimpleNamespace(plan_name=plan_name) if plan_name else None)
        perm = MagicMock()
        perm.check_context_owner = AsyncMock(return_value=existing)
        service = MagicMock()
        service.update_context = AsyncMock(return_value=existing)

        with patch("services.permission_service.PermissionService", return_value=perm):
            await update_context(
                existing.id,
                ContextUpdate(**fields),
                {"user_id": "owner-1", "sub": "owner-1"},
                service,
                db,
            )
        return service, db

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_name", _NON_XL)
    async def test_refused_below_xl(self, plan_name: str) -> None:
        with pytest.raises(FeatureNotAvailableError) as exc_info:
            await self._put(plan_name, self._existing(is_public=False), is_public=True)

        assert exc_info.value.details["feature"] == "public_contexts"
        assert "XL" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_allowed_on_xl(self) -> None:
        service, _ = await self._put("promax", self._existing(is_public=False), is_public=True)
        assert service.update_context.await_args.kwargs["is_public"] is True

    @pytest.mark.asyncio
    async def test_missing_workspace_row_fails_closed(self) -> None:
        with pytest.raises(FeatureNotAvailableError):
            await self._put(None, self._existing(is_public=False), is_public=True)

    @pytest.mark.asyncio
    async def test_already_public_pro_context_keeps_serving(self) -> None:
        """Block-new-only: a public L context can still be saved with
        ``is_public=True`` (the settings form re-sends the flag) — the plan is
        not consulted for a context that is already public."""
        service, db = await self._put("pro", self._existing(is_public=True), is_public=True)
        service.update_context.assert_awaited_once()
        db.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_stays_on_pro(self) -> None:
        """``is_private=False`` is the SHARED gate (``allows_shared_contexts``),
        still satisfied by L — the public re-map does not drag it along."""
        service, _ = await self._put("pro", self._existing(is_public=False), is_private=False)
        service.update_context.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /workspaces/{ws}/members/{user}/credentials/api-keys — bound public key
# ---------------------------------------------------------------------------


class TestBoundPublicKeyCreate:
    @pytest.fixture
    def client(self):
        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    def _arrange(self, monkeypatch, plan_name: str) -> MagicMock:
        from api.routes import member_credentials as mc

        user = {"user_id": "member-1", "email": "m@test.invalid", "role": "user", "sub": "m"}

        async def _get_user():
            return user

        app.dependency_overrides[get_user_from_api_key_or_session] = _get_user

        ctx = SimpleNamespace(
            id=uuid.uuid4(), workspace_id=_WS, is_public=True, created_by="member-1"
        )
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        # 1) bound context lookup, 2) workspace (plan) lookup
        fake_db.execute = AsyncMock(
            side_effect=[_result(one=ctx), _result(one=SimpleNamespace(plan_name=plan_name))]
        )

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        new_key = MagicMock()
        new_key.id = 42
        new_key.name = "public-key"
        new_key.key_prefix = "kagura_pub"
        new_key.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        new_key.expires_at = None
        new_key.bound_context_id = ctx.id
        new_key.visibility_expires_at = None
        new_key.last_used_at = None
        mgr = MagicMock()
        mgr.create_key = AsyncMock(return_value=("kagura_PLAINTEXT", new_key))
        monkeypatch.setattr(mc, "APIKeyManager", lambda db: mgr)
        self._ctx = ctx
        return mgr

    def _mint(self, client: TestClient):
        return client.post(
            f"/api/v1/workspaces/{_WS}/members/member-1/credentials/api-keys",
            json={"name": "public-key", "bound_context_id": str(self._ctx.id)},
        )

    @pytest.mark.parametrize("plan_name", _NON_XL)
    def test_refused_below_xl(self, client, monkeypatch, plan_name: str) -> None:
        """FEAT-001 with the registry-derived tier — not the uniform AUTH-101
        "Insufficient permissions" text, which would hide the upgrade path."""
        mgr = self._arrange(monkeypatch, plan_name)
        r = self._mint(client)
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "FEAT-001"
        assert "public_contexts" in body["message"] and "XL" in body["message"]
        assert "Insufficient permissions" not in r.text
        mgr.create_key.assert_not_awaited()

    def test_allowed_on_xl(self, client, monkeypatch) -> None:
        mgr = self._arrange(monkeypatch, "promax")
        r = self._mint(client)
        assert r.status_code == 201, r.text
        assert mgr.create_key.await_args.kwargs["bound_context_id"] == self._ctx.id


# ---------------------------------------------------------------------------
# Hardcoded-name clean-ups taken along
# ---------------------------------------------------------------------------


class TestInvitationGateIsRegistryDriven:
    """``invitations.py`` gates on ``has_feature(plan, "team_invitations")``:
    XL is allowed without any name list, and the refusal text names the tier
    from the registry rather than a hardcoded "Pro"."""

    @pytest.fixture
    def client(self):
        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    def _arrange(self, monkeypatch, plan_name: str) -> MagicMock:
        from api.routes import invitations as inv_mod
        from auth import programmatic_workspace_auth as mod

        user = {
            "user_id": "owner-key",
            "email": "owner@api",
            "role": "user",
            "current_workspace_id": _WS,
            "api_key_workspace_id": None,
        }

        async def _get_user():
            return user

        app.dependency_overrides[get_user_from_api_key_or_session] = _get_user

        perm = AsyncMock()
        perm.check_workspace_owner.return_value = MagicMock(role="owner")
        monkeypatch.setattr(mod, "PermissionService", lambda db: perm)

        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        ws_result = MagicMock()
        ws_result.scalar_one.return_value = SimpleNamespace(plan_name=plan_name)
        fake_db.execute = AsyncMock(return_value=ws_result)

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        inv = MagicMock()
        inv.id = 1
        inv.workspace_id = _WS
        inv.token = "tok"
        inv.email = "x@example.com"
        inv.role = "member"
        inv.invited_by = "owner-key"
        inv.expires_at = None
        inv.accepted_at = None
        inv.accepted_by = None
        inv.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        inv.is_expired.return_value = False
        inv.is_accepted.return_value = False
        inv.allowed_context_ids = None
        svc = MagicMock()
        svc.create_invitation = AsyncMock(return_value=inv)
        monkeypatch.setattr(inv_mod, "InvitationService", lambda db: svc)
        monkeypatch.setattr(
            "services.quota_service.QuotaService.check_member_quota",
            AsyncMock(return_value=(True, None)),
        )
        return svc

    def _invite(self, client: TestClient):
        return client.post(
            f"/api/v1/workspaces/{_WS}/invitations",
            json={"role": "member", "email": "x@example.com"},
        )

    def test_promax_may_invite(self, client, monkeypatch) -> None:
        svc = self._arrange(monkeypatch, "promax")
        r = self._invite(client)
        assert r.status_code in (200, 201), r.text
        svc.create_invitation.assert_awaited_once()

    def test_basic_refusal_names_the_registry_tier(self, client, monkeypatch) -> None:
        from config.plan_tiers import get_plan_tier

        svc = self._arrange(monkeypatch, "basic")
        r = self._invite(client)
        assert r.status_code == 403
        assert get_plan_tier("pro").display_name in r.text
        assert "Pro plan" not in r.text
        svc.create_invitation.assert_not_awaited()


@pytest.mark.parametrize(
    ("old_plan", "new_plan", "expected"),
    [
        ("promax", "free", True),
        ("basic", "free", True),
        ("pro", "free", True),
        ("pro", "promax", False),
        ("pro", "basic", False),
        ("free", "basic", False),
        ("free", "free", False),
    ],
)
def test_admin_plan_change_disables_reranking_when_the_feature_is_lost(
    old_plan: str, new_plan: str, expected: bool
) -> None:
    """``admin_plans.py``: reranking is switched off when the NEW tier lacks
    the feature the OLD tier had — derived from the registry, not from a
    ``== "free"`` special case."""
    from api.routes.admin_plans import _loses_feature

    assert _loses_feature(old_plan, new_plan, "reranking") is expected
