"""Route-level integration tests for the public-bound API key flow.

Issue #630 — closes the gate2 QA Lead yellow verdict from #626 / PR #628.

Exercises the composition layer that unit tests miss: header parsing →
pre-auth bucket → IDOR guard → context lookup → per-key bucket →
UsageStats attribution → response — end-to-end via ``httpx.AsyncClient``
+ ``ASGITransport(app=app)`` against the real Postgres test DB and the
live Redis container.

Each test seeds a unique ``uuid4()`` context_id, so the Redis bucket keys
(``public_search:{ctx}:minute``, ``public_search_pre_auth:{ctx}:minute``,
``public_bound_key:{key_id}:minute``) never collide across tests — no
``flushdb`` and no order coupling.

``API_KEY_SECRET`` is already set deterministically by
``backend/tests/integration/conftest.py:42`` (``setdefault`` to an ephemeral
test secret); no per-test monkeypatch needed.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from auth.api_keys import APIKeyManager
from db.redis import get_redis_client
from models.auth import Context, UsageStats, Workspace, WorkspaceMember

from ._admin_helpers import make_user, make_workspace

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_public_context(
    db: AsyncSession, *, is_public: bool = True
) -> tuple[Workspace, Context, str]:
    """Seed User + Workspace (pro plan) + Context, return ``(ws, ctx, user_id)``.

    Pro plan is required because ``bound_public_calls_per_minute > 0`` is
    a precondition for bound-key minting / per-key bucket assertions.
    """
    user = make_user()
    db.add(user)
    await db.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
    db.add(ws)
    await db.flush()

    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.user_id, role="owner"))

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"pub-{uuid4().hex[:8]}",
        created_by=user.user_id,
        is_private=False,
        is_public=is_public,
    )
    db.add(ctx)
    await db.commit()

    return ws, ctx, user.user_id


async def _mint_bound_key(
    db: AsyncSession,
    user_id: str,
    context_id: UUID,
    *,
    name: str = "pub-key",
) -> tuple[str, int]:
    """Create a public-bound API key. Returns ``(plaintext, key_id)``."""
    plaintext, row = await APIKeyManager(db).create_key(
        name=name,
        user_id=user_id,
        bound_context_id=context_id,
    )
    await db.commit()
    return plaintext, row.id


async def _mint_owner_key(db: AsyncSession, user_id: str, *, name: str = "owner-key") -> str:
    """Create an owner-scoped (non-bound, non-workspace) API key. Returns plaintext."""
    plaintext, _ = await APIKeyManager(db).create_key(name=name, user_id=user_id)
    await db.commit()
    return plaintext


async def _bucket_count(redis_key: str) -> int:
    """Read a Redis counter directly. Returns 0 when the key is missing."""
    client = get_redis_client()
    raw = await client.get(redis_key)
    return int(raw) if raw is not None else 0


async def _bucket_reset(*redis_keys: str) -> None:
    """Delete the given Redis keys so per-key bucket assertions start from a
    clean baseline. The unique-``uuid4()`` context_id strategy already isolates
    the ``public_search:{ctx}:minute`` and ``public_search_pre_auth:{ctx}:minute``
    buckets, but ``public_bound_key:{key_id}:minute`` keys on the integer
    ``api_keys.id`` — which can collide with stale Redis state from a prior run
    when the test DB is recreated with ``TRUNCATE ... RESTART IDENTITY``.
    """
    if not redis_keys:
        return
    client = get_redis_client()
    await client.delete(*redis_keys)


def _error_detail(response_json: dict) -> str:
    """Return the human-readable error detail across both response shapes.

    ``HTTPException`` (used by ``_resolve_public_attribution``) emits
    ``{"detail": "..."}``. ``MemoryCloudException`` subclasses
    (``AuthorizationError``, ``NotFoundException``) flow through
    ``memory_cloud_exception_handler`` which emits
    ``{"error": ..., "message": ..., "details": {}}``. Tests assert on
    semantic content rather than wire shape — this helper centralizes
    the dispatch so a single error-handler refactor doesn't ripple
    across every case.
    """
    return response_json.get("detail") or response_json.get("message") or ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_search_service(monkeypatch):
    """Replace ``SearchService.hybrid_search`` with a no-op returning ``[]``.

    Lets the 200-path tests reach ``log_usage`` and ``PublicSearchResponse``
    without a live Qdrant / embedding-service dependency. Tests that need
    payload-shape assertions can override this via further patching.
    """

    async def _stub(self, **kwargs):  # noqa: ARG001 — signature mirrors caller
        return []

    monkeypatch.setattr(
        "services.search_service.SearchService.hybrid_search",
        _stub,
    )
    return _stub


@pytest_asyncio.fixture
async def client():
    """ASGI httpx client. Lifespan startup is skipped because
    ``get_redis_client()`` and ``db.base``'s engine factory are lazy — they
    cold-start on first use, which is sufficient for the request-level
    contract exercised here.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Case 1 — Anonymous (no Authorization)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case1_anonymous_public_search_increments_anonymous_bucket(
    client: AsyncClient,
    db_session: AsyncSession,
    stub_search_service,
):
    """No Authorization + public ctx → 200, ``public_search:{ctx}:minute``
    incremented; pre-auth bucket NOT touched (header absent).
    """
    _, ctx, _ = await _seed_public_context(db_session)

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        json={"query": "hello", "limit": 5},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["context_id"] == str(ctx.id)
    assert body["count"] == 0
    assert body["results"] == []

    assert await _bucket_count(f"public_search:{ctx.id}:minute") == 1
    assert await _bucket_count(f"public_search_pre_auth:{ctx.id}:minute") == 0


# ---------------------------------------------------------------------------
# Case 2 — Invalid Bearer (pre-auth bucket guards verify_key DB-DoS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case2_invalid_bearer_increments_pre_auth_bucket(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Bearer invalid + public ctx → 401. Pre-auth bucket is incremented
    BEFORE ``verify_key`` runs — protects the DB from invalid-key flood
    amplification and prevents the endpoint from acting as a key oracle.
    """
    _, ctx, _ = await _seed_public_context(db_session)

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        headers={"Authorization": "Bearer kagura_not_a_real_key_xyz"},
        json={"query": "hello"},
    )

    assert response.status_code == 401
    assert "Invalid or expired API key" in _error_detail(response.json())

    assert await _bucket_count(f"public_search_pre_auth:{ctx.id}:minute") == 1
    # Anonymous bucket is untouched — Authorization header was present.
    assert await _bucket_count(f"public_search:{ctx.id}:minute") == 0


# ---------------------------------------------------------------------------
# Case 3 — Owner-scoped (non-bound) valid key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case3_owner_scoped_key_rejected_with_not_bound_403(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Owner-scoped valid key on public endpoint → 403 ``not bound``.

    The error detail must NOT leak the Bearer material or key prefix
    (#115 leak-prevention contract).
    """
    _, ctx, user_id = await _seed_public_context(db_session)
    plaintext = await _mint_owner_key(db_session, user_id)

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"query": "hello"},
    )

    assert response.status_code == 403
    detail = _error_detail(response.json())
    assert "not bound" in detail.lower()
    assert plaintext[:16] not in detail
    assert "Bearer" not in detail


# ---------------------------------------------------------------------------
# Case 4 — Bound key, matching context (success + attribution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case4_bound_key_matching_succeeds_with_per_key_bucket_and_attribution(
    client: AsyncClient,
    db_session: AsyncSession,
    stub_search_service,
):
    """Bound key, matching context_id → 200.

    Per-key bucket is the ONLY bucket charged (attributed traffic does not
    deplete the shared anonymous bucket). UsageStats writes the key id so
    attribution survives key deletion via soft reference.
    """
    _, ctx, user_id = await _seed_public_context(db_session)
    plaintext, key_id = await _mint_bound_key(db_session, user_id, ctx.id)

    # Clear any stale per-key bucket state from a prior run that wrote to
    # the same auto-increment id (TRUNCATE ... RESTART IDENTITY on the
    # test DB resets api_keys.id but Redis state has a 60s TTL).
    await _bucket_reset(
        f"public_bound_key:{key_id}:minute",
        f"public_search_pre_auth:{ctx.id}:minute",
    )

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"query": "hello", "limit": 5},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["context_id"] == str(ctx.id)

    # Per-key bucket charged exactly once.
    assert await _bucket_count(f"public_bound_key:{key_id}:minute") == 1
    # Anonymous bucket NOT charged — attributed callers use their own bucket.
    assert await _bucket_count(f"public_search:{ctx.id}:minute") == 0
    # Pre-auth bucket charged once (every Authorization header pays this).
    assert await _bucket_count(f"public_search_pre_auth:{ctx.id}:minute") == 1

    # UsageStats row written with api_key_id attribution.
    result = await db_session.execute(select(UsageStats).where(UsageStats.api_key_id == key_id))
    row = result.scalar_one()
    assert row.status_code == 200
    assert row.endpoint == f"/api/v1/public/{ctx.id}/search"
    # UsageStats.context_id is a UUID column — log_usage receives ``str`` and
    # SQLAlchemy coerces it to UUID on insert. Compare via str() so the assertion
    # is robust regardless of which side does the cast.
    assert str(row.context_id) == str(ctx.id)
    assert row.user_id == user_id


# ---------------------------------------------------------------------------
# Case 5 — Bound key, mismatching context (CWE-639 IDOR core)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case5_bound_key_mismatching_returns_bound_scope_violation(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Bound key minted for ctx_A → request hits ctx_B (both public) → 403
    ``BOUND_SCOPE_VIOLATION``. The response body must NOT leak the bound
    context id or the key material — leakage would re-introduce the
    side-channel the IDOR guard exists to block.
    """
    _, ctx_a, user_id = await _seed_public_context(db_session)
    _, ctx_b, _ = await _seed_public_context(db_session)
    plaintext, key_id = await _mint_bound_key(db_session, user_id, ctx_a.id, name="key-a")

    response = await client.post(
        f"/api/v1/public/{ctx_b.id}/search",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"query": "hello"},
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "BOUND_SCOPE_VIOLATION" in detail
    # Leak-prevention: the response must not name ctx_a (the bound context).
    assert str(ctx_a.id) not in detail
    # And must not echo the key plaintext / prefix.
    assert plaintext[:16] not in detail
    assert "Bearer" not in detail

    # Per-key bucket NOT incremented — denied at the IDOR gate, before
    # ``check_bound_key_rate_limit``. The pre-auth bucket IS incremented
    # (any Authorization header pays for it, regardless of validity).
    assert await _bucket_count(f"public_bound_key:{key_id}:minute") == 0
    assert await _bucket_count(f"public_search_pre_auth:{ctx_b.id}:minute") == 1


# ---------------------------------------------------------------------------
# Case 6 — is_public flipped false post-creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case6_is_public_flipped_false_post_creation_denies(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Bound key minted while ctx public → ctx.is_public flipped false →
    subsequent request returns 403 ``not public``.

    The binding row STAYS so the owner can re-enable later; access is
    blocked while ``is_public`` is false.
    """
    _, ctx, user_id = await _seed_public_context(db_session)
    plaintext, _ = await _mint_bound_key(db_session, user_id, ctx.id)

    # Flip is_public to False after key mint.
    ctx_row = await db_session.get(Context, ctx.id)
    assert ctx_row is not None
    ctx_row.is_public = False
    await db_session.commit()

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"query": "hello"},
    )

    assert response.status_code == 403
    assert "not public" in _error_detail(response.json()).lower()


# ---------------------------------------------------------------------------
# Case 7 — Context vanishes between auth phase and search phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case7_context_vanishes_mid_request_returns_4xx(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """Bound key valid + context row vanishes between the IDOR auth phase
    and the route's ``db.get(Context, ...)`` lookup.

    Simulated via monkeypatch on ``AsyncSession.get``: the auth phase calls
    ``APIKeyManager.verify_key`` which only reads ``APIKey`` by hash (NOT
    via ``db.get``), so the patch only fires when the route reaches step 2
    (context lookup). The contract: a row that disappears after the IDOR
    check must yield a 4xx — never a 5xx, never a 200 with a stale schema.
    """
    _, ctx, user_id = await _seed_public_context(db_session)
    plaintext, _ = await _mint_bound_key(db_session, user_id, ctx.id)

    import sqlalchemy.ext.asyncio as _async_module

    original_get = _async_module.AsyncSession.get

    async def _patched_get(self, entity, ident, *args, **kwargs):
        if entity is Context:
            return None
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(_async_module.AsyncSession, "get", _patched_get)

    response = await client.post(
        f"/api/v1/public/{ctx.id}/search",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"query": "hello"},
    )

    # NotFoundException → 404 via the MemoryCloudException handler. 403 is
    # also acceptable; what matters is the 4xx class — bound-key callers
    # must NOT see a 5xx (or a 200) when the context disappears mid-request.
    assert 400 <= response.status_code < 500, response.text
    assert response.status_code in (403, 404), response.text


# ---------------------------------------------------------------------------
# Symmetry — /info endpoint applies the same IDOR guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_info_endpoint_mismatching_bound_key_returns_bound_scope_violation(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """The ``/info`` endpoint enforces the same IDOR guard as ``/search``.

    Closes the half-test trap where future refactors could weaken the
    guard on ``/info`` (no rate-limit / no attribution → easier to miss)
    while ``/search`` stays correct.
    """
    _, ctx_a, user_id = await _seed_public_context(db_session)
    _, ctx_b, _ = await _seed_public_context(db_session)
    plaintext, _ = await _mint_bound_key(db_session, user_id, ctx_a.id, name="info-key-a")

    response = await client.get(
        f"/api/v1/public/{ctx_b.id}/info",
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert response.status_code == 403
    assert "BOUND_SCOPE_VIOLATION" in _error_detail(response.json())
