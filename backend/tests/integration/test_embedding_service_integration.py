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
    * ``services.email_service._email_service`` singleton — replaced with
      a ``MagicMock`` whose ``send_embedding_spend_alert`` is an
      ``AsyncMock`` we can assert against.

Real surface (do not mock):
    * ``EmbeddingService._prepare_spend_cap_gate``, ``has_byok_key``,
      ``_get_user_api_key`` — the chain under test.
    * ``EmbeddingSpendCapService`` — full instance, real cap arithmetic.
    * Postgres test DB via the session-scoped ``db_session`` fixture.
    * ``fakeredis.aioredis.FakeRedis`` — speaks the Redis wire protocol;
      ``get_cache`` / ``incrby_counter`` / ``SETNX`` all work unmodified.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession

# Test-only API_KEY_SECRET so any ExternalAPIKey decrypt path that falls back
# to Fernet has a stable key (mirrors backend/tests/integration/conftest.py:42).
os.environ.setdefault("API_KEY_SECRET", "integration-test-api-key-secret-not-for-prod")

from db import redis as redis_module  # noqa: E402
from models.auth import ExternalAPIKey, User, Workspace  # noqa: E402
from services import email_service as email_module  # noqa: E402
from services.embedding_service import EmbeddingService  # noqa: E402
from services.embedding_spend_cap_service import _MICRO_USD  # noqa: E402
from utils.encryption import get_encryptor  # noqa: E402
from utils.exceptions import EmbeddingSpendCapExceeded  # noqa: E402


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


async def _commit_user(db: AsyncSession, *, user_id: str | None = None) -> User:
    uid = user_id or f"u_{uuid4().hex[:8]}"
    user = User(
        email=f"{uid}@test.invalid",
        user_id=uid,
        name="Cap Test User",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )
    db.add(user)
    await db.flush()
    return user


async def _commit_workspace(
    db: AsyncSession,
    *,
    owner_user_id: str,
    plan_name: str = "free",
    embedding_daily_cap_usd: Decimal | None = None,
    embedding_monthly_cap_usd: Decimal | None = None,
) -> Workspace:
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name=plan_name,
        owner_user_id=owner_user_id,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
        embedding_daily_cap_usd=embedding_daily_cap_usd,
        embedding_monthly_cap_usd=embedding_monthly_cap_usd,
    )
    db.add(ws)
    await db.flush()
    return ws


async def _commit_byok_key(
    db: AsyncSession,
    *,
    workspace_id,
    user_id: str,
    context_id=None,
    provider: str = "openai",
) -> ExternalAPIKey:
    encrypted = get_encryptor().encrypt("sk-test-fake-byok-key")
    key = ExternalAPIKey(
        key_name=f"k_{uuid4().hex[:8]}",
        provider=provider,
        encrypted_value=encrypted,
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        enabled=True,
    )
    db.add(key)
    await db.flush()
    return key


@pytest_asyncio.fixture
async def byok_workspace(db_session: AsyncSession):
    """Workspace + BYOK key, no cap configured (uncapped)."""
    user = await _commit_user(db_session)
    ws = await _commit_workspace(db_session, owner_user_id=user.user_id)
    await _commit_byok_key(db_session, workspace_id=ws.id, user_id=user.user_id)
    await db_session.commit()
    yield ws
    await db_session.rollback()


@pytest_asyncio.fixture
async def byok_workspace_with_cap(db_session: AsyncSession):
    """Workspace + BYOK key with a $10 daily cap, no monthly cap."""
    user = await _commit_user(db_session)
    ws = await _commit_workspace(
        db_session,
        owner_user_id=user.user_id,
        embedding_daily_cap_usd=Decimal("10.000000"),
    )
    await _commit_byok_key(db_session, workspace_id=ws.id, user_id=user.user_id)
    await db_session.commit()
    yield ws
    await db_session.rollback()


@pytest_asyncio.fixture
async def unbound_workspace(db_session: AsyncSession):
    """Workspace without any ``ExternalAPIKey`` row — platform-fallback path."""
    user = await _commit_user(db_session)
    ws = await _commit_workspace(db_session, owner_user_id=user.user_id)
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
        from datetime import UTC, datetime

        bucket = datetime.now(UTC).strftime("%Y-%m-%d")
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
