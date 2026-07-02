"""Full-chain integration tests for ``EmbeddingService.embed_with_usage`` (#714).

Closes the coverage gap left by PR #711 (#709) — the per-workspace embedding
spend cap was unit-tested in isolation in
``test_embedding_spend_cap_service.py`` but the chain through
``_prepare_spend_cap_gate`` (with its BYOK probe, cap pre-check, and
post-call ``record_spend_from_tokens``) was only covered indirectly.

Each test exercises the real Postgres test DB, ``fakeredis.aioredis.FakeRedis``
in place of Redis, and ``AsyncMock`` stubs for the OpenAI SDK and the
``LLMPricingService.compute_cost_usd`` helper. Implementation under test
is unchanged — these tests are debt cleanup, not a behavior change.

Mock surface (intentional, minimal):
    * ``services.embedding_service.AsyncOpenAI`` — synthetic 512-dim
      response with ``usage.prompt_tokens = 100``.
    * ``services.llm_pricing_service.LLMPricingService.compute_cost_usd`` —
      returns ``0.5`` USD per call so cap-trip math is deterministic.
    * ``services.email_service._default_email_service`` singleton —
      replaced with a ``MagicMock`` whose ``send_embedding_spend_alert``
      is an ``AsyncMock`` we can assert against.

Real surface (do not mock):
    * ``EmbeddingService._prepare_spend_cap_gate``, ``has_byok_key``,
      ``_get_user_api_key`` — the chain under test.
    * ``EmbeddingSpendCapService`` — full instance, real cap arithmetic.
    * Postgres test DB via the session-scoped ``db_session`` fixture.
    * ``fakeredis.aioredis.FakeRedis`` — speaks the Redis wire protocol;
      ``get_cache`` / ``incrby_counter`` / ``SETNX`` all work unmodified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession

from db import redis as redis_module
from models.auth import ExternalAPIKey, Workspace
from services import email_service as email_module
from services.embedding_service import EmbeddingService
from services.embedding_spend_cap_service import _MICRO_USD
from utils.encryption import get_encryptor
from utils.exceptions import EmbeddingSpendCapExceeded, RedisError

from ._admin_helpers import make_user

# --- Redis ----------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    """Bind a fresh ``FakeRedis`` to ``db.redis._redis_client`` for one test.

    ``get_redis_client()`` returns the module-level singleton, so every
    helper that touches Redis (``get_cache``, ``incrby_counter``,
    ``SETNX``) goes through this fake without further patching. Teardown
    flushes the DB and restores the singleton so the next test rebinds.
    """
    fake = FakeRedis(decode_responses=True)
    prev = redis_module._redis_client
    redis_module._redis_client = fake
    try:
        yield fake
    finally:
        await fake.flushall()
        await fake.aclose()
        redis_module._redis_client = prev


# --- OpenAI ---------------------------------------------------------------


@pytest.fixture
def mock_openai_client():
    """Stub AsyncOpenAI exposing ``.embeddings.create`` as an ``AsyncMock``.

    Default response: 512-dim vector and 100 prompt_tokens. Tests can
    override via ``mock_openai_client.embeddings.create.side_effect``.
    """
    client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(embedding=[0.1] * 512)]
    response.usage = MagicMock(prompt_tokens=100, total_tokens=100)
    client.embeddings = MagicMock()
    client.embeddings.create = AsyncMock(return_value=response)
    return client


@pytest.fixture
def patch_openai(mock_openai_client):
    """Replace ``services.embedding_service.AsyncOpenAI`` with a factory."""
    with patch(
        "services.embedding_service.AsyncOpenAI",
        return_value=mock_openai_client,
    ):
        yield mock_openai_client


# --- Pricing --------------------------------------------------------------


@pytest.fixture
def patch_pricing():
    """Pin ``LLMPricingService.compute_cost_usd`` to $0.50/call.

    With this constant, a workspace with a $10 daily cap takes exactly
    20 calls to exhaust. Combined with pre-seeded Redis counters the
    tests can reach any %-of-cap state in one extra call.
    """
    with patch(
        "services.llm_pricing_service.LLMPricingService.compute_cost_usd",
        new_callable=AsyncMock,
    ) as mock_cost:
        mock_cost.return_value = 0.5
        yield mock_cost


# --- Email ----------------------------------------------------------------


@pytest.fixture
def mock_email_service():
    """Swap the email singleton with a ``MagicMock`` for alert assertions."""
    fake = MagicMock()
    fake.send_embedding_spend_alert = AsyncMock(return_value=True)
    prev = email_module._default_email_service
    email_module._default_email_service = fake
    try:
        yield fake
    finally:
        email_module._default_email_service = prev


# --- DB rows --------------------------------------------------------------


def _build_workspace(
    *,
    owner_user_id: str,
    embedding_daily_cap_usd: Decimal | None = None,
) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner_user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
        embedding_daily_cap_usd=embedding_daily_cap_usd,
    )


def _build_byok_key(*, workspace_id, user_id: str) -> ExternalAPIKey:
    return ExternalAPIKey(
        key_name=f"k_{uuid4().hex[:8]}",
        provider="openai",
        encrypted_value=get_encryptor().encrypt("sk-test-fake-byok-key"),
        user_id=user_id,
        workspace_id=workspace_id,
        enabled=True,
    )


@pytest_asyncio.fixture
async def byok_workspace_with_cap(db_session: AsyncSession):
    """Workspace + BYOK key with a $10 daily cap, no monthly cap."""
    user = make_user()
    ws = _build_workspace(
        owner_user_id=user.user_id,
        embedding_daily_cap_usd=Decimal("10.000000"),
    )
    db_session.add_all([user, ws])
    # FK dep: external_api_keys.workspace_id → workspaces.id. The FK uses a
    # raw UUID value (no SQLAlchemy relationship), so UoW can't sort the
    # INSERTs — flush the parent first.
    await db_session.flush()
    db_session.add(_build_byok_key(workspace_id=ws.id, user_id=user.user_id))
    await db_session.commit()
    yield ws
    await db_session.rollback()


@pytest_asyncio.fixture
async def unbound_workspace(db_session: AsyncSession):
    """Workspace without any ``ExternalAPIKey`` row — platform-fallback path."""
    user = make_user()
    ws = _build_workspace(owner_user_id=user.user_id)
    db_session.add_all([user, ws])
    await db_session.commit()
    yield ws
    await db_session.rollback()


# --- Tests ----------------------------------------------------------------


class TestEmbedWithUsageCapGate:
    """Integration tests for embed_with_usage → cap_service chain (#714)."""

    @pytest.mark.asyncio
    async def test_byok_under_cap_passes_and_records_spend(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """BYOK workspace, $10 daily cap, no prior spend → first call succeeds,
        counter rises to $0.50 (5% of cap), no alert fires."""
        svc = EmbeddingService(db_session)
        vector, tokens = await svc.embed_with_usage(
            text="hello world",
            user_id=byok_workspace_with_cap.owner_user_id,
            workspace_id=str(byok_workspace_with_cap.id),
        )

        assert len(vector) == 512
        assert tokens == 100

        # OpenAI was actually called (gate did not short-circuit)
        patch_openai.embeddings.create.assert_awaited_once()

        # Counter reflects $0.50 (= 500_000 micro-USD)
        counter_key_prefix = f"embed_spend:{byok_workspace_with_cap.id}:daily:"
        keys = [k async for k in fake_redis.scan_iter(match=f"{counter_key_prefix}*")]
        assert len(keys) == 1
        assert int(await fake_redis.get(keys[0])) == int(Decimal("0.5") * _MICRO_USD)

        # 5% is below the 80% threshold → no alert
        mock_email_service.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_byok_crossing_80_percent_fires_alert_once(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """Pre-seed $7.50 spent (75% of $10 cap), one call → $8 (80%) →
        alert fires exactly once. Second call same day → no second alert
        (Redis SETNX dedup)."""
        frozen_now = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        with patch(
            "services.embedding_spend_cap_service.utcnow",
            return_value=frozen_now,
        ):
            bucket = frozen_now.strftime("%Y-%m-%d")
            counter_key = f"embed_spend:{byok_workspace_with_cap.id}:daily:{bucket}"
            await fake_redis.set(counter_key, str(int(Decimal("7.5") * _MICRO_USD)))

            svc = EmbeddingService(db_session)

            # First call lands at exactly 80% → alert
            await svc.embed_with_usage(
                text="call-1",
                user_id=byok_workspace_with_cap.owner_user_id,
                workspace_id=str(byok_workspace_with_cap.id),
            )
            assert int(await fake_redis.get(counter_key)) == int(Decimal("8.0") * _MICRO_USD)
            mock_email_service.send_embedding_spend_alert.assert_awaited_once()
            kwargs = mock_email_service.send_embedding_spend_alert.await_args.kwargs
            assert kwargs["threshold_pct"] == 80
            assert kwargs["period"] == "daily"

            # Second call same day lands at 85% — still ≥80% but SETNX has
            # already claimed the dedup key, so no second alert.
            await svc.embed_with_usage(
                text="call-2",
                user_id=byok_workspace_with_cap.owner_user_id,
                workspace_id=str(byok_workspace_with_cap.id),
            )
            assert int(await fake_redis.get(counter_key)) == int(Decimal("8.5") * _MICRO_USD)
            # Still exactly one alert send across both calls
            assert mock_email_service.send_embedding_spend_alert.await_count == 1

            # Dedup key exists with TTL > 0
            dedup_key = f"embed_spend_alert:{byok_workspace_with_cap.id}:daily:{bucket}:80"
            assert await fake_redis.get(dedup_key) == "1"
            assert await fake_redis.ttl(dedup_key) > 0

    @pytest.mark.asyncio
    async def test_byok_at_cap_raises_before_api_call(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """Pre-seed $10 spent (100% of $10 cap). Call must raise
        EmbeddingSpendCapExceeded BEFORE any OpenAI call, and the
        counter must NOT have been incremented past 100%."""
        bucket = datetime.now(UTC).strftime("%Y-%m-%d")
        counter_key = f"embed_spend:{byok_workspace_with_cap.id}:daily:{bucket}"
        await fake_redis.set(counter_key, str(int(Decimal("10.0") * _MICRO_USD)))

        svc = EmbeddingService(db_session)
        with pytest.raises(EmbeddingSpendCapExceeded) as exc_info:
            await svc.embed_with_usage(
                text="over-cap",
                user_id=byok_workspace_with_cap.owner_user_id,
                workspace_id=str(byok_workspace_with_cap.id),
            )

        # The exception carries the structured fields the route handler
        # uses to render a QUOTA-002 / 429 response.
        assert exc_info.value.details["period"] == "daily"
        assert exc_info.value.details["cap_usd"] == 10.0
        assert exc_info.value.status_code == 429

        # OpenAI was never called — the gate fired first.
        patch_openai.embeddings.create.assert_not_called()

        # Counter is untouched (still $10).
        assert int(await fake_redis.get(counter_key)) == int(Decimal("10.0") * _MICRO_USD)

    @pytest.mark.asyncio
    async def test_platform_fallback_skips_cap_path(
        self,
        db_session: AsyncSession,
        unbound_workspace: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
        monkeypatch,
    ):
        """Workspace WITHOUT a BYOK row → ``has_byok_key`` returns False →
        ``_prepare_spend_cap_gate`` returns (None, None) → embed succeeds
        using OPENAI_API_KEY env, no counter increment, no alert.

        This is the BYOK-only-cap contract documented in
        ``_prepare_spend_cap_gate`` (issue #708 drain-attack mitigation):
        platform-key fallback is NOT subject to the per-workspace cap.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "test-env-key")

        svc = EmbeddingService(db_session)
        vector, _ = await svc.embed_with_usage(
            text="platform call",
            user_id=unbound_workspace.owner_user_id,
            workspace_id=str(unbound_workspace.id),
        )

        assert len(vector) == 512
        patch_openai.embeddings.create.assert_awaited_once()

        # No counter key created — cap path was never entered.
        counter_prefix = f"embed_spend:{unbound_workspace.id}:"
        keys = [k async for k in fake_redis.scan_iter(match=f"{counter_prefix}*")]
        assert keys == []

        # And no alert.
        mock_email_service.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_hosted_provider_skips_cap_path(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """``provider=self_hosted`` short-circuits the cap gate regardless of
        BYOK row presence — self-hosted is local, no real provider cost.

        ``EmbeddingService.__init__`` derives ``provider`` from the model
        registry, so we override ``svc.provider`` post-construction to
        exercise the self-hosted branch without registering a fake model.
        """
        svc = EmbeddingService(db_session)
        # ``provider`` is derived from the model in __init__; override it
        # directly so we exercise the self-hosted branch without needing a
        # model registry entry.
        svc.provider = "self_hosted"
        # Bypass the self-hosted HTTP probe that runs on first use of _get_client.
        svc._self_hosted_verified = True

        # The shared patch_openai fixture replaced ``AsyncOpenAI``, so the
        # self-hosted branch's ``AsyncOpenAI(base_url=..., api_key=...)``
        # call returns the same stub. That's intentional — we don't run a
        # real self-hosted backend in tests.
        vector, _ = await svc.embed_with_usage(
            text="self_hosted call",
            user_id=byok_workspace_with_cap.owner_user_id,
            workspace_id=str(byok_workspace_with_cap.id),
        )
        assert len(vector) == 512

        # No counter key — cap path was skipped before any Redis op.
        counter_prefix = f"embed_spend:{byok_workspace_with_cap.id}:"
        keys = [k async for k in fake_redis.scan_iter(match=f"{counter_prefix}*")]
        assert keys == []
        mock_email_service.send_embedding_spend_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_disappears_mid_call_falls_through(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """Race: BYOK probe sees the workspace row, then the row is deleted
        before ``load_workspace`` runs. The gate must return (None, None)
        and embed must proceed — the call is no longer attributable to a
        capped workspace, but should not 500.

        The race can't be reproduced via real DELETE because
        ``ExternalAPIKey.workspace_id`` has ``ondelete=CASCADE`` — the
        BYOK row would vanish with the workspace. So we patch
        ``load_workspace`` to return None for one call, simulating the
        narrow window where the BYOK SELECT and the Workspace SELECT
        are both real but the row vanished between them.
        """
        with patch(
            "services.embedding_spend_cap_service.EmbeddingSpendCapService.load_workspace",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_load:
            svc = EmbeddingService(db_session)
            vector, _ = await svc.embed_with_usage(
                text="raced call",
                user_id=byok_workspace_with_cap.owner_user_id,
                workspace_id=str(byok_workspace_with_cap.id),
            )

        assert len(vector) == 512
        mock_load.assert_awaited_once()
        # No spend recorded because cap_workspace is None
        counter_prefix = f"embed_spend:{byok_workspace_with_cap.id}:"
        keys = [k async for k in fake_redis.scan_iter(match=f"{counter_prefix}*")]
        assert keys == []
        # Call still succeeded against the mocked OpenAI
        patch_openai.embeddings.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_redis_outage_during_check_fails_open(
        self,
        db_session: AsyncSession,
        byok_workspace_with_cap: Workspace,
        fake_redis: FakeRedis,
        patch_openai,
        patch_pricing,
        mock_email_service,
    ):
        """``check_cap_or_raise`` swallows any Redis read error and treats
        spend as 0 — the embed must succeed even if Redis is down at the
        moment of the cap check. The post-call INCRBY may also fail but
        does not raise either; the chain must remain non-raising.

        Patch ``get_cache`` on the cap-service module so the pre-check
        side errors out, and patch ``incrby_counter`` to error so the
        post-call record is also exercised on the failure path.
        """
        with (
            patch(
                "services.embedding_spend_cap_service.get_cache",
                new_callable=AsyncMock,
                side_effect=RuntimeError("redis down"),
            ),
            patch(
                "services.embedding_spend_cap_service.incrby_counter",
                new_callable=AsyncMock,
                side_effect=RedisError("redis down"),
            ),
        ):
            svc = EmbeddingService(db_session)
            vector, _ = await svc.embed_with_usage(
                text="redis-out call",
                user_id=byok_workspace_with_cap.owner_user_id,
                workspace_id=str(byok_workspace_with_cap.id),
            )

        assert len(vector) == 512
        # OpenAI was called (fail-open: cap check failed → treat as $0 spend)
        patch_openai.embeddings.create.assert_awaited_once()
        # No alert (record failed → no threshold computation)
        mock_email_service.send_embedding_spend_alert.assert_not_called()
