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
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError

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
# POST /contexts — is_private=False ("shared", stays on L)
# ---------------------------------------------------------------------------


class TestContextCreateShared:
    """#1561: the REST shared gate is ``has_feature(plan, "shared_contexts")``
    and refuses with ``FeatureNotAvailableError.for_feature`` — FEAT-001 (403),
    tier name from the registry — instead of the old fixed "Pro plan" text."""

    async def _post(self, plan_name: str | None):
        from api.routes.contexts import ContextCreate, create_context

        created = SimpleNamespace(
            id=uuid.uuid4(),
            name="ctx",
            display_name=None,
            description=None,
            summary=None,
            usage_guide=None,
            is_default=False,
            is_private=False,
            sleep_mode="skip",
            created_by="owner-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        service = MagicMock()
        service.create_context = AsyncMock(return_value=created)
        # 1) workspace (plan) lookup, 2) search-config lookup for the response
        service.db.execute = AsyncMock(
            side_effect=[_result(one=SimpleNamespace(plan_name=plan_name)), _result(one=None)]
        )

        with patch(
            "services.quota_service.QuotaService.check_context_creation_allowed",
            new=AsyncMock(return_value=(True, None)),
        ):
            response = await create_context(
                ContextCreate(name="ctx", is_private=False),
                {"user_id": "owner-1", "sub": "owner-1", "current_workspace_id": _WS},
                service,
            )
        return response, service

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_name", ["free", "basic"])
    async def test_refused_below_l(self, plan_name: str) -> None:
        from config.plan_tiers import get_plan_tier

        with pytest.raises(FeatureNotAvailableError) as exc_info:
            await self._post(plan_name)

        assert exc_info.value.status_code == 403
        assert exc_info.value.details["feature"] == "shared_contexts"
        assert f"Upgrade to {get_plan_tier('pro').display_name} plan" in exc_info.value.message
        assert "Pro plan" not in exc_info.value.message

    @pytest.mark.asyncio
    async def test_allowed_on_l(self) -> None:
        response, service = await self._post("pro")
        assert response.is_private is False
        assert service.create_context.await_args.kwargs["is_private"] is False


# ---------------------------------------------------------------------------
# PUT /contexts/{id} — is_public=True ("set_public")
# ---------------------------------------------------------------------------


class TestContextSetPublic:
    def _existing(self, *, is_public: bool, is_private: bool = False) -> SimpleNamespace:
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
            is_private=is_private,
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
        service, _ = await self._put(
            "pro", self._existing(is_public=False, is_private=True), is_private=False
        )
        service.update_context.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_name", ["free", "basic"])
    async def test_shared_refused_below_l(self, plan_name: str) -> None:
        """#1561: the update gate now answers FEAT-001 (403) with the registry
        tier name — it used to be a 400 (route-translated ``ValidationError``)
        with fixed "Pro plan" text. #1583: it is the private → shared
        TRANSITION that is refused, and both tiers read as display names."""
        from config.plan_tiers import get_plan_tier

        with pytest.raises(FeatureNotAvailableError) as exc_info:
            await self._put(
                plan_name, self._existing(is_public=False, is_private=True), is_private=False
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.error_code == "FEAT-001"
        assert exc_info.value.details["feature"] == "shared_contexts"
        assert f"on {get_plan_tier(plan_name).display_name} plan" in exc_info.value.message
        assert f"Upgrade to {get_plan_tier('pro').display_name} plan" in exc_info.value.message
        assert "Pro plan" not in exc_info.value.message


# ---------------------------------------------------------------------------
# PUT /contexts/{id} — the shared gate fires on the transition only (#1583)
# ---------------------------------------------------------------------------


class TestContextSharedGateIsTransitionOnly:
    """Block-new-only, same as ``is_public``: a legacy shared context on a plan
    that no longer includes ``shared_contexts`` stays editable, and a request
    that re-submits the stored ``is_private`` is a no-op, never a refusal."""

    # Same PUT harness as the public-gate class (not inherited: that would
    # collect its tests twice).
    _existing = TestContextSetPublic._existing
    _put = TestContextSetPublic._put

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_name", ["free", "basic"])
    async def test_already_shared_context_accepts_a_no_op_is_private_false(
        self, plan_name: str
    ) -> None:
        service, db = await self._put(
            plan_name, self._existing(is_public=False), is_private=False, summary="new"
        )
        kwargs = service.update_context.await_args.kwargs
        assert (kwargs["is_private"], kwargs["summary"]) == (False, "new")
        # The plan is not consulted for a context that is already shared.
        db.get.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored_private", [True, False])
    async def test_sleep_mode_only_payload_is_never_gated(self, stored_private: bool) -> None:
        service, db = await self._put(
            "basic",
            self._existing(is_public=False, is_private=stored_private),
            sleep_mode="full",
        )
        kwargs = service.update_context.await_args.kwargs
        assert (kwargs["sleep_mode"], kwargs["is_private"]) == ("full", None)
        db.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_to_private_is_open_on_every_plan(self) -> None:
        service, db = await self._put("free", self._existing(is_public=False), is_private=True)
        assert service.update_context.await_args.kwargs["is_private"] is True
        db.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resource_id_lock_ignores_a_no_op_is_private_true(self) -> None:
        """#242's "Cannot change to private" is about the shared → private
        move; re-submitting ``is_private=True`` on a private context that
        carries a ``resource_id`` must not block an unrelated edit."""
        existing = self._existing(is_public=False, is_private=True)
        existing.resource_id = "products"
        service, _ = await self._put("basic", existing, is_private=True, summary="new")
        service.update_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resource_id_lock_still_refuses_shared_to_private(self) -> None:
        existing = self._existing(is_public=True)  # shared, resource_id="products"
        with pytest.raises(HTTPException) as exc_info:
            await self._put("pro", existing, is_private=True)
        assert exc_info.value.status_code == 400
        assert "Cannot change to private" in str(exc_info.value.detail)


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

    @pytest.mark.asyncio
    async def test_member_regenerate_cannot_mint_a_bound_key(self) -> None:
        """#1551 grandfathering check for the OTHER rotation route: the
        member-credentials regenerate selects ``workspace_id == <ws>`` keys only
        (bound keys have ``workspace_id IS NULL``) and never passes
        ``bound_context_id`` — so it can neither rotate nor create a bound key,
        and needs no create gate."""
        import inspect

        from api.routes import member_credentials as mc

        old_key = SimpleNamespace(id=7, name="ws-key", revoked_at=None)
        new_key = SimpleNamespace(id=8, key_prefix="kagura_new")
        db = MagicMock()
        db.execute = AsyncMock(return_value=_result(one=old_key))
        db.commit = AsyncMock()
        mgr = MagicMock()
        mgr.create_key = AsyncMock(return_value=("kagura_PLAIN", new_key))

        with (
            patch("api.routes.member_credentials.check_permission", new=AsyncMock()),
            patch("api.routes.member_credentials.MemberCredentialsService"),
            patch("api.routes.member_credentials.APIKeyManager", return_value=mgr),
        ):
            r = await mc.regenerate_api_key(
                _WS, "member-1", {"user_id": "member-1", "sub": "m"}, db
            )

        assert r.key == "kagura_PLAIN"
        kwargs = mgr.create_key.await_args.kwargs
        assert kwargs["workspace_id"] == _WS
        assert "bound_context_id" not in kwargs
        assert old_key.revoked_at is not None
        # The lookup itself excludes bound keys (workspace_id IS NULL rows).
        assert "APIKey.workspace_id == workspace_id" in inspect.getsource(mc.regenerate_api_key)


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

    def test_invitation_plan_refusal_is_feat001_not_a_raw_403(self, client, monkeypatch) -> None:
        """#1644 S1: the status does not move; the non-semantic ``HTTP-403``
        placeholder is replaced by the documented ``FEAT-001`` envelope."""
        from config.plan_tiers import get_plan_tier, required_plan_name

        self._arrange(monkeypatch, "basic")
        r = self._invite(client)

        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "FEAT-001"
        details = body["details"]
        assert details["gate"] == "plan"
        assert details["feature"] == "team_invitations"
        assert details["current_plan"] == "basic"
        required = required_plan_name("team_invitations")
        assert details["required_plan"] == required
        assert details["required_plan_display"] == get_plan_tier(required).display_name

    def test_invitation_seat_cap_is_quota001_with_quota_type_members(
        self, client, monkeypatch
    ) -> None:
        """#1644 S2: the route no longer re-wraps the cap into a bare 429 — the
        service's own envelope (and its details) reach the client intact."""
        from config.plan_tiers import quota_gate_details

        svc = self._arrange(monkeypatch, "promax")
        monkeypatch.setattr(
            "services.quota_service.QuotaService.check_member_quota",
            AsyncMock(
                side_effect=QuotaExceededError(
                    "Member limit reached (50 seats).",
                    **quota_gate_details(
                        "promax",
                        "members",
                        current=50,
                        limit=50,
                        required_plan=None,
                        feature="team_invitations",
                    ),
                )
            ),
        )

        r = self._invite(client)

        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "QUOTA-001"
        details = body["details"]
        assert details["gate"] == "quota"
        assert details["quota_type"] == "members"
        assert (details["current"], details["limit"]) == (50, 50)
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
