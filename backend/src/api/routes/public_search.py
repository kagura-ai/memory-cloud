"""Public Search API routes.

Issue #238: Public REST API for external access to public contexts.

Provides search endpoints for public contexts with schema-aware responses.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager, VerifiedKey
from auth.dependencies import get_api_key, get_current_user_optional
from config.plan_tiers import get_plan_tier
from db.base import get_db
from db.redis import incrby_counter
from models.auth import Context, Workspace
from services.resource_lookup import get_latest_schema
from services.search_service import SearchService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import APIKeyError, AuthorizationError, NotFoundException, RateLimitError
from utils.logger import get_logger
from utils.usage_logger import log_usage

logger = get_logger(__name__)

router = APIRouter(prefix="/public", tags=["public-search"])


# ============================================================================
# Pydantic Models
# ============================================================================


class PublicSearchRequest(BaseModel):
    """Public search request."""

    query: str = Field(..., min_length=1, max_length=500, description="Search query")
    limit: int = Field(10, ge=1, le=100, description="Maximum results (default: 10, max: 100)")
    use_rerank: bool = Field(False, description="Enable reranking for better results")
    search_mode: str = Field(
        "hybrid",
        pattern="^(hybrid|semantic|keyword)$",
        description="Search mode: hybrid (default), semantic, keyword",
    )


class PublicSearchResult(BaseModel):
    """Single search result with schema-aware formatting."""

    memory_id: str
    content: str
    score: float
    metadata: dict
    highlighted: dict | None = None


class PublicSearchResponse(BaseModel):
    """Public search response."""

    status: str = "success"
    query: str
    results: list[PublicSearchResult]
    count: int
    context_id: str
    resource_id: str | None = None
    schema_version: int | None = None
    # #1515: set only when the semantic arm was unavailable and this search was
    # served from BM25 alone. It matters more here than on /recall: `score`
    # silently changes basis (normalized 0-1 hybrid score vs raw unbounded
    # BM25), and an integration thresholding on it would otherwise ingest a
    # different distribution as if it were healthy — with a 200, so nothing
    # signals a retry either.
    degraded: bool | None = None
    degraded_reason: str | None = None


# ============================================================================
# Rate Limiting
# ============================================================================


async def check_public_search_rate_limit(
    context_id: UUID,
    user_id: str | None,
    limit_per_minute: int = 50,
) -> None:
    """Check rate limit for public search API.

    Args:
        context_id: Context ID
        user_id: User ID (None for anonymous/public token)
        limit_per_minute: Rate limit (default: 50/min for public, unlimited for workspace members)

    Raises:
        RateLimitError: If rate limit exceeded
    """
    # Skip rate limit check for authenticated workspace members
    if user_id:
        logger.debug("rate_limit_skipped_for_workspace_member", user_id=user_id)
        return

    # Apply rate limit for anonymous/public token access. ``incrby_counter``
    # post-checks the TTL via ``TTL`` and sets ``EXPIRE`` whenever absent —
    # avoiding the race in the older ``increment_counter`` where two
    # concurrent first-increments could leave the key with no TTL (counter
    # grows forever, locking the bucket into permanent 429). Same primitive
    # the new #626 bound-key bucket uses.
    redis_key = f"public_search:{context_id}:minute"

    try:
        current_count = await incrby_counter(redis_key, amount=1, ttl=60)

        if current_count > limit_per_minute:
            logger.warning(
                "public_search_rate_limit_exceeded",
                context_id=context_id,
                current=current_count,
                limit=limit_per_minute,
            )
            raise RateLimitError(
                message=f"Public search rate limit exceeded: {current_count}/{limit_per_minute} per minute",
                retry_after=60,
            )

        logger.debug(
            "public_search_rate_limit_checked",
            context_id=context_id,
            current=current_count,
            limit=limit_per_minute,
        )

    except RateLimitError:
        raise
    except Exception as e:
        # Redis errors should not block search (fail open)
        logger.error("redis_rate_limit_check_failed", error=str(e))


async def check_bound_key_rate_limit(key_id: int, limit_per_minute: int) -> None:
    """Per-key rate-limit bucket for public-bound API key access (#626).

    Uses a dedicated Redis bucket keyed on the API key id, so attributed
    consumers do not share the anonymous ``public_search:{ctx}:minute``
    bucket — and one bound key being scraped does not exhaust the quota
    for anonymous users or for the owner's other bound keys.

    Args:
        key_id: ``api_keys.id`` of the public-bound key.
        limit_per_minute: Per-key per-minute quota from the workspace plan
            (``PlanTier.bound_public_calls_per_minute``). When 0, every
            request is treated as quota-exceeded so the route returns 429
            instead of silently allowing unlimited traffic — keys should
            not have been mintable on such a plan in the first place
            (tier gate at create time), but defensive enforcement here
            keeps the contract honest.

    Raises:
        RateLimitError: If the per-minute bucket is exhausted. Redis
            errors are logged and swallowed (fail-open), mirroring the
            anonymous bucket's behavior.
    """
    if limit_per_minute <= 0:
        raise RateLimitError(
            message="Public-bound API key quota not provisioned for this plan",
            retry_after=60,
        )

    redis_key = f"public_bound_key:{key_id}:minute"
    # Use ``incrby_counter`` rather than ``increment_counter`` here: the
    # older variant only sets TTL when ``count == 1``, so two concurrent
    # first-increments leave the key with no expiration (counter grows
    # forever). ``incrby_counter`` post-checks the TTL via ``TTL`` and
    # sets it whenever absent — race-window stays advisory-bounded.
    try:
        current_count = await incrby_counter(redis_key, amount=1, ttl=60)
        if current_count > limit_per_minute:
            logger.warning(
                "public_bound_key_rate_limit_exceeded",
                key_id=key_id,
                current=current_count,
                limit=limit_per_minute,
            )
            raise RateLimitError(
                message=(
                    f"Public-bound API key rate limit exceeded: "
                    f"{current_count}/{limit_per_minute} per minute"
                ),
                retry_after=60,
            )
        logger.debug(
            "public_bound_key_rate_limit_checked",
            key_id=key_id,
            current=current_count,
            limit=limit_per_minute,
        )
    except RateLimitError:
        raise
    except Exception as e:
        logger.error("redis_bound_key_rate_limit_check_failed", error=str(e))


async def check_pre_auth_rate_limit(context_id: UUID, limit_per_minute: int = 200) -> None:
    """Pre-auth rate-limit bucket — protects against invalid-key DB DoS.

    Without this gate, any caller can hammer the public endpoint with
    ``Authorization: Bearer <random>`` headers: each request reaches
    ``APIKeyManager.verify_key`` (a SELECT on the unique ``key_hash``
    index) BEFORE the anonymous shared 50/min bucket is consulted —
    turning the route into an unauthenticated DB-backed key-verification
    oracle and amplifying the DB load per attacker-request.

    Strategy: a cheap Redis check keyed on ``context_id`` runs BEFORE
    ``verify_key``. The default 200/min/context is 4× the anonymous 50/
    min cap, so legitimate bound-key callers (whose per-key quotas are
    typically 100/min on PRO) are not the binding constraint — the
    per-key bucket trips first. Invalid-key spammers, however, get
    throttled at 200/min total per context regardless of how many
    distinct keys they cycle through.

    Args:
        context_id: Target context UUID — bucket scope.
        limit_per_minute: Pre-auth requests/minute cap (default 200).

    Raises:
        RateLimitError: If the per-minute bucket is exhausted. Redis
            errors are logged and swallowed (fail-open).
    """
    redis_key = f"public_search_pre_auth:{context_id}:minute"
    try:
        current_count = await incrby_counter(redis_key, amount=1, ttl=60)
        if current_count > limit_per_minute:
            logger.warning(
                "public_search_pre_auth_rate_limit_exceeded",
                context_id=context_id,
                current=current_count,
                limit=limit_per_minute,
            )
            raise RateLimitError(
                message=(
                    f"Pre-auth rate limit exceeded: {current_count}/{limit_per_minute} per minute"
                ),
                retry_after=60,
            )
    except RateLimitError:
        raise
    except Exception as e:
        logger.error("redis_pre_auth_rate_limit_check_failed", error=str(e))


async def _resolve_public_attribution(
    api_key: str | None,
    context_id: UUID,
    db: AsyncSession,
) -> VerifiedKey | None:
    """Resolve an optional Bearer API key for public endpoint attribution.

    Issue #626. Returns ``None`` for anonymous (no Authorization header) so
    the caller falls through to the existing rate-limit bucket. Returns a
    ``VerifiedKey`` for a valid public-bound key whose binding matches the
    URL ``context_id``. Raises ``HTTPException`` directly for any failure
    so the IDOR / authn / authz contract is enforced BEFORE the route
    reads any context data.

    Status codes (matches the route docstring's ``Errors:`` section):

    - ``401`` for authentication failure (invalid / expired / revoked key).
    - ``403`` for authorization failure (key authenticates but is owner /
      workspace-scoped, or is bound to a different context).
    """
    if api_key is None:
        return None

    verified = await APIKeyManager(db).verify_key(api_key)
    if verified is None:
        # Authentication failure — invalid hash, revoked, or expired.
        raise APIKeyError("Invalid or expired API key")

    if verified.bound_context_id is None:
        # Valid key (owner / workspace-scoped) without a public binding.
        raise AuthorizationError(
            message=(
                "API key is not bound to a public context; "
                "use a public-bound key or omit Authorization for anonymous access"
            )
        )

    if verified.bound_context_id != context_id:
        # CWE-639 (IDOR) — key is bound to a different context than the URL
        # asks for. 403 rather than 404 so we don't leak URL-context existence.
        logger.warning(
            "public_bound_key_scope_violation",
            requested_context_id=str(context_id),
            bound_context_id=str(verified.bound_context_id),
        )
        # Intentional override of AuthorizationError's default uniform message:
        # the BOUND_SCOPE_VIOLATION marker is a documented client signal and
        # leaks no resource existence (the bound_context_id stays log-only, and
        # the handler strips details). Do NOT "normalize" this back to the
        # default message — a test asserts this marker is visible.
        raise AuthorizationError(
            message="API key is bound to a different context (BOUND_SCOPE_VIOLATION)"
        )

    return verified


# ============================================================================
# Public Search Endpoint
# ============================================================================


@router.post("/{context_id}/search", response_model=PublicSearchResponse)
async def public_search(
    context_id: UUID,
    request: PublicSearchRequest,
    user: dict | None = Depends(get_current_user_optional),
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Search public context with schema-aware response formatting.

    Issue #238: Public REST API for external systems (EC inventory, docs, etc.).
    Issue #626: Optional API-key attribution via ``Authorization: Bearer
    <kagura_…>``. A bound key gets a per-key rate-limit bucket and audit
    attribution; an unbound key (owner / workspace-scoped) is rejected so
    callers don't accidentally route their primary credentials through a
    public path. Anonymous access continues to work without any header.

    Authentication:
        - Workspace member session (authenticated access, no rate limit)
        - Public-bound API key (#626) — per-key rate limit, audit-attributed
        - Anonymous access (rate limited to 50 req/min per context, shared)

    Rate Limits:
        - Anonymous access: 50 requests/minute per context (shared bucket)
        - Public-bound key: per-key bucket from workspace plan tier
          (``bound_public_calls_per_minute``)
        - Workspace members: No limit (uses normal workspace quota)

    Request:
        POST /api/v1/public/{context_id}/search
        Headers:
            Authorization: Bearer kagura_…    (optional; required only for
                                               attributed/bound-key access)
        Body:
        {
            "query": "商品名 価格",
            "limit": 10,
            "use_rerank": false
        }

    Response:
        {
            "status": "success",
            "query": "商品名 価格",
            "results": [
                {
                    "memory_id": "...",
                    "content": "商品名: ..., 価格: ...",
                    "score": 0.95,
                    "metadata": {...},
                    "highlighted": {...}
                }
            ],
            "count": 5,
            "context_id": "...",
            "resource_id": "ec_products",
            "schema_version": 1
        }

    Errors:
        - 401: API key supplied but invalid / expired
        - 403: Context is not public, OR API key is not public-bound, OR
               API key is bound to a different context (CWE-639 IDOR)
        - 404: Context not found
        - 429: Rate limit exceeded (anonymous or per-key bucket)
    """
    start_time = utcnow()

    # 1a. Pre-auth rate limit when an Authorization header is supplied.
    # Without this, an attacker could bypass the anonymous 50/min bucket
    # by sending invalid-key Authorization headers — each request would
    # reach ``verify_key`` (a DB SELECT) before any rate-limit check,
    # turning the endpoint into a DB-DoS amplifier and a key-verification
    # oracle. The cheap Redis check here runs BEFORE ``verify_key`` so
    # invalid-key floods are bounded.
    if api_key is not None:
        await check_pre_auth_rate_limit(context_id)

    # 1b. Resolve optional API-key attribution BEFORE reading context data,
    # so a key bound to a different context cannot leak existence of the
    # URL context via the response shape.
    bound_key = await _resolve_public_attribution(api_key, context_id, db)

    logger.info(
        "public_search_request",
        context_id=context_id,
        query=request.query,
        limit=request.limit,
        has_user=user is not None,
        has_bound_key=bound_key is not None,
        bound_key_id=bound_key.id if bound_key else None,
    )

    # 2. Get context and verify it's public
    context = await db.get(Context, context_id)
    if not context:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        # When the binding row points to a context that the owner has since
        # flipped to is_public=false, we deny here exactly like anonymous —
        # the binding row stays so the owner can re-enable, but access is
        # blocked while the public flag is off.
        raise AuthorizationError("This context is not public")

    # SECURITY: For authenticated users, verify workspace boundary
    # Issue #268: Workspace boundary violation prevention
    # Authenticated users can only search public contexts in their own workspace
    # Anonymous users can search any public context (rate limited)
    # Public-bound keys (#626) are exempt — the binding itself IS the
    # authorization, and the IDOR check above already ensured the key
    # belongs to this exact context.
    if user is not None and bound_key is None:
        current_workspace_id = user.get("current_workspace_id")

        # Authenticated user must have workspace selected
        if not current_workspace_id:
            raise HTTPException(
                status_code=400, detail="No workspace selected. Please select an workspace first."
            )

        # Verify context belongs to user's current workspace
        if context.workspace_id != current_workspace_id:
            raise AuthorizationError(
                message="This public context belongs to a different workspace. Please switch workspaces or use anonymous access."
            )

    # 3. Rate limit dispatch by principal type.
    # The Workspace fetch is deferred to after this gate so a flood of
    # 429-bound anonymous requests don't burn a DB lookup each. Bound-key
    # path needs the plan tier (so it fetches workspace inline); other
    # paths fetch lazily below, only on requests that survive the bucket.
    workspace: Workspace | None = None
    if bound_key is not None:
        workspace = await db.get(Workspace, context.workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Context workspace not found",
            )
        plan = get_plan_tier(workspace.plan_name or "free")
        await check_bound_key_rate_limit(bound_key.id, plan.bound_public_calls_per_minute)
    elif user is None:
        # Anonymous shared bucket — existing behavior preserved.
        # Workspace lookup is deferred to step 4 below: under abuse, a
        # 429 returns BEFORE any Workspace SELECT.
        await check_public_search_rate_limit(context_id, None)
    # else: workspace member session → no rate limit (existing behavior).

    # 4. Resolve workspace lazily if not already fetched above. Required
    # for the embedding-service caller user_id (search_user_id).
    if workspace is None:
        workspace = await db.get(Workspace, context.workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Context workspace not found",
            )

    # 5. Hoist usage-log attribution + caller id once so the success and
    # error paths below share one definition.
    log_user_id: str = (
        str(bound_key.user_id)
        if bound_key is not None
        else str(user.get("user_id", "anonymous"))
        if user
        else "anonymous"
    )
    log_attribution = {
        "context_id": str(context_id),
        "workspace_id": str(context.workspace_id),
        "api_key_id": bound_key.id if bound_key is not None else None,
    }

    search_service = SearchService(db)
    try:
        # Workspace owner's user_id is used as the embedding-service caller so
        # API-key resolution lands on the owner's keys, not the public caller.
        owner_id = workspace.owner_user_id
        search_user_id: str = str(owner_id) if owner_id is not None else "system"

        # Reranking is permitted for workspace members (existing behavior) and
        # for attributed bound-key callers (#626); anonymous traffic stays
        # rerank-disabled to avoid exposing rerank cost to unbounded callers.
        degradation: dict[str, Any] = {}
        results = await search_service.hybrid_search(
            query=request.query,
            user_id=search_user_id,
            workspace_id=str(context.workspace_id),
            context_id=str(context_id),
            k=request.limit,
            use_rerank=request.use_rerank and (user is not None or bound_key is not None),
            search_mode=request.search_mode,
            degradation=degradation,
        )

        schema = (
            await get_latest_schema(db, context.workspace_id, context.resource_id)
            if context.resource_id is not None
            else None
        )

        formatted_results = [
            PublicSearchResult(
                memory_id=str(result["id"]),
                content=result.get("payload", {}).get(
                    "summary", result.get("payload", {}).get("content", "")
                ),
                score=result["score"],
                metadata=result.get("payload", {}),
                highlighted=None,  # TODO: Implement highlighting in future
            )
            for result in results
        ]

        response_time_ms = int((utcnow() - start_time).total_seconds() * 1000)
        await log_usage(
            db=db,
            user_id=log_user_id,
            endpoint=f"/api/v1/public/{context_id}/search",
            method="POST",
            status_code=200,
            response_time_ms=response_time_ms,
            **log_attribution,
        )

        logger.info(
            "public_search_completed",
            context_id=context_id,
            result_count=len(formatted_results),
            has_schema=schema is not None,
            response_time_ms=response_time_ms,
        )

        # Extract values for type safety (SQLAlchemy Column types need explicit handling)
        ctx_resource_id = context.resource_id
        schema_ver: int | None = int(schema.schema_version) if schema else None  # type: ignore[arg-type]

        return PublicSearchResponse(
            status="success",
            query=request.query,
            results=formatted_results,
            count=len(formatted_results),
            context_id=str(context_id),
            resource_id=str(ctx_resource_id) if ctx_resource_id is not None else None,
            schema_version=schema_ver,
            # #1515: None (omitted from the payload) unless the search degraded.
            degraded=degradation.get("degraded"),
            degraded_reason=degradation.get("reason"),
        )

    except Exception as e:
        response_time_ms = int((utcnow() - start_time).total_seconds() * 1000)
        await log_usage(
            db=db,
            user_id=log_user_id,
            endpoint=f"/api/v1/public/{context_id}/search",
            method="POST",
            status_code=500,
            response_time_ms=response_time_ms,
            **log_attribution,
        )

        logger.error(
            "public_search_failed",
            context_id=context_id,
            error=str(e),
        )
        # Do not leak the raw exception message to the (potentially
        # anonymous) client — DB / provider / library errors can carry
        # column names, connection strings, file paths, etc. The full
        # message is in the logger.error above for operator triage.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal search error",
        ) from e


# ============================================================================
# Public Context Info Endpoint
# ============================================================================


@router.get("/{context_id}/info")
async def get_public_context_info(
    context_id: UUID,
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get public context metadata and schema information.

    Returns basic information about the public context including:
    - Context name and description
    - Resource schema (if resource-backed)
    - Statistics (memory count, last updated)

    Issue #626: An optional ``Authorization: Bearer <api_key>`` is accepted
    for symmetry with ``/search``. The same IDOR guard applies — a key
    bound to a different context is rejected with 403 — but no rate limit
    or audit attribution is applied here because ``/info`` returns only
    static metadata (no memory contents).

    Args:
        context_id: Context UUID
        api_key: Optional public-bound API key (Issue #626).
        db: Database session

    Returns:
        Public context metadata

    Errors:
        - 401: API key supplied but invalid / expired
        - 403: Context is not public, OR API key is not public-bound, OR
               API key is bound to a different context (CWE-639 IDOR)
        - 404: Context not found
    """
    # Issue #626: pre-auth rate limit + IDOR guard run before context
    # lookup, symmetry with /search. The pre-auth gate protects
    # ``verify_key`` from invalid-key DB-DoS amplification.
    if api_key is not None:
        await check_pre_auth_rate_limit(context_id)
    await _resolve_public_attribution(api_key, context_id, db)

    # Get context
    context = await db.get(Context, context_id)
    if not context:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        raise AuthorizationError("This context is not public")

    # Load schema if resource-backed.
    schema = (
        await get_latest_schema(db, context.workspace_id, context.resource_id)
        if context.resource_id is not None
        else None
    )

    return {
        "context_id": str(context_id),
        "name": context.name,
        "display_name": context.display_name,
        "description": context.description,
        "is_public": context.is_public,
        "resource_id": context.resource_id,
        "schema": {
            "version": schema.schema_version,
            "field_definitions": schema.field_definitions,
            "updated_at": to_utc_iso(schema.updated_at),
        }
        if schema
        else None,
        "created_at": to_utc_iso(context.created_at),
    }
