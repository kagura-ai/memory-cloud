"""Public Search API routes.

Issue #238: Public REST API for external access to public contexts.

Provides search endpoints for public contexts with schema-aware responses.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import (
    APIKeyManager,
    VerifiedKey,
    bound_key_owner_holds_context_workspace,
)
from auth.dependencies import get_api_key
from config.plan_tiers import get_plan_tier
from db.base import get_db
from db.redis import incrby_counter
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.scope import public_rows
from models.auth import Context, Workspace
from models.memory import Memory
from models.public_mcp import PublicMcpEndpoint
from services.capacity_lock import ensure_not_capacity_locked
from services.plan_suspension import ensure_public_serving_allowed
from services.public_search_usage import (
    NULL_RECORD,
    SearchRecord,
    VisitorHeaderSource,
    public_search_recording_enabled,
    start_bound_record,
    visitor_header_source,
)
from services.public_usage_budget import (
    PublicUsageBudgetUnavailable,
    charge_public_usage,
    public_usage_budget_limit,
)
from services.recall_timings import RecallTimings
from services.resource_lookup import get_latest_schema
from services.search_service import SearchService
from services.workspace_slot_gate import ensure_slot_active
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    APIKeyError,
    AuthorizationError,
    EmbeddingSpendCapExceeded,
    NotFoundException,
    PublicUsageBudgetExhaustedError,
    PublicUsageBudgetUnavailableError,
    RateLimitError,
    TemporarilyUnavailableError,
)
from utils.logger import get_logger
from utils.query_log import query_log_fields
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
    # #3035: the public search never reranks, whoever calls it. The field stays
    # so that clients which still send it keep working.
    use_rerank: bool = Field(
        False, deprecated=True, description="Ignored: the public search never reranks."
    )
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
    limit_per_minute: int = 50,
) -> bool:
    """Count an anonymous search in the context's shared per-minute bucket.

    Every search without a public-bound key is anonymous here, whether or
    not the request carries a session cookie (#2823).

    Args:
        context_id: Context ID
        limit_per_minute: Rate limit (default: 50/min per context)

    Returns:
        ``True`` when the bucket counted the request; ``False`` when Redis
        could not be read (the bucket fails open, and the caller decides:
        a search charged to the public usage budget is refused then, #2805).

    Raises:
        RateLimitError: If rate limit exceeded
    """
    # ``incrby_counter``
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
                reason="anonymous_limit",
            )

        logger.debug(
            "public_search_rate_limit_checked",
            context_id=context_id,
            current=current_count,
            limit=limit_per_minute,
        )
        return True

    except RateLimitError:
        raise
    except Exception as e:
        # Redis errors should not block search (fail open) — except on the
        # path charged to the public usage budget, where the caller refuses.
        logger.error("redis_rate_limit_check_failed", error=str(e))
        return False


async def _has_live_publication(db: AsyncSession, context_id: UUID) -> bool:
    """Whether the context has a live (not revoked) public MCP endpoint (#2805).

    One ``EXISTS`` on the partial unique index of live publications.
    """
    return bool(
        await db.scalar(
            select(
                exists().where(
                    PublicMcpEndpoint.context_id == context_id,
                    PublicMcpEndpoint.revoked_at.is_(None),
                )
            )
        )
    )


async def _charge_public_usage_budget(
    db: AsyncSession, context: Context, *, bucket_counted: bool
) -> None:
    """Charge an anonymous search to the workspace's public usage budget (#2805).

    Only when the operator switch is on and the context has a live public MCP
    publication (the rows the MCP endpoint serves); otherwise nothing is
    counted, as before. On the charged path a unit is charged only to a
    request the per-minute bucket admitted: an unreadable bucket refuses.

    Raises:
        PublicUsageBudgetUnavailableError: The bucket was not read, or the
            charge failed (fail closed, 503).
        PublicUsageBudgetExhaustedError: The period's calls are used (429).
    """
    limit = public_usage_budget_limit()
    if limit is None:
        return
    if not await _has_live_publication(db, context.id):
        return
    if not bucket_counted:
        raise PublicUsageBudgetUnavailableError()
    # The charge checks out a connection of its own. Release the request's
    # first (it has only read so far; ``expire_on_commit=False`` keeps the
    # loaded context and workspace usable), so a request never holds one
    # connection while it waits for a second: enough concurrent anonymous
    # searches would otherwise drain the pool and all fail closed together
    # (hold-and-wait), stalling every other request meanwhile. The search
    # checks a connection out again afterwards.
    await db.commit()
    try:
        charge = await charge_public_usage(context.workspace_id, limit=limit, db=db)
    except PublicUsageBudgetUnavailable as exc:
        raise PublicUsageBudgetUnavailableError() from exc
    if not charge.admitted:
        raise PublicUsageBudgetExhaustedError(
            retry_after=charge.retry_after_seconds or 60,
        )


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
            reason="key_not_provisioned",
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
                reason="key_limit",
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
                reason="pre_auth_limit",
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

    - ``401`` for authentication failure (invalid / expired / revoked key,
      or a public-bound key whose owner does not belong to the live
      workspace of its context: the same answer).
    - ``403`` for authorization failure (key authenticates but is owner /
      workspace-scoped, or is bound to a different context).
    """
    if api_key is None:
        return None

    verified = await APIKeyManager(db).verify_key(api_key)
    if verified is None:
        # Authentication failure — invalid hash, revoked, or expired.
        raise APIKeyError("Invalid or expired API key")

    # A public-bound key serves only while its owner belongs to the live
    # workspace of its context, as a workspace-scoped key does (#2928). Checked
    # before anything else reads the key, with an unknown key's answer; the
    # request's rollback drops the ``last_used_at`` that ``verify_key`` wrote.
    if verified.bound_context_id is not None and not (
        await bound_key_owner_holds_context_workspace(
            db, user_id=verified.user_id, context_id=verified.bound_context_id
        )
    ):
        logger.warning(
            "public_bound_key_rejected_owner_not_in_workspace",
            key_prefix=verified.key_prefix,
            requested_context_id=str(context_id),
        )
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
# Results: each row once, only the context's live rows (#3035, K4a)
# ============================================================================


def _row_uuid(memory_id: str) -> UUID | None:
    try:
        return UUID(memory_id)
    except ValueError:
        return None


async def _filter_results(
    db: AsyncSession,
    *,
    context: Context,
    results: list[PublicSearchResult],
    limit: int,
    publication: ServingEndpoint | None = None,
) -> tuple[list[PublicSearchResult], tuple[str, ...] | None, int]:
    """Answer each row once, and only the context's live rows (#3035, K4a).

    De-duplicates by ``memory_id`` (the first position wins; always, also when
    the read below fails), then reads the remaining rows once by primary key,
    in a savepoint on the request session, and keeps those that live in the
    context's workspace and context and are not deleted, in search order, cut
    to ``limit``. A ``memory_id`` that is not a UUID names no row and is
    dropped. When the read fails the de-duplicated results are served
    unfiltered (one warning with the error class only): a transient database
    error does not fail the search.

    With ``publication`` (a recorded search) the same read also tells, per
    row, whether the public MCP endpoint serves it (``public_rows``, the MCP
    predicate), for the event's documents.

    Returns:
        The kept results; one document id per kept result, in response order
        (the row's ``resource_doc_id`` when the publication serves it, else
        ``""``, so later ranks stay true), or ``None`` without a publication
        or when the read failed; and how many results de-duplication and the
        read removed (the cut to ``limit`` is not counted).
    """
    seen: set[str] = set()
    unique: list[PublicSearchResult] = []
    for result in results:
        if result.memory_id not in seen:
            seen.add(result.memory_id)
            unique.append(result)
    ids = {result.memory_id: _row_uuid(result.memory_id) for result in unique}
    wanted = [row_id for row_id in ids.values() if row_id is not None]
    if not wanted:
        kept: list[PublicSearchResult] = []
        return kept, (() if publication is not None else None), len(results)
    columns: list[Any] = [Memory.id, Memory.resource_doc_id]
    if publication is not None:
        columns.append(public_rows(publication).label("in_publication"))
    statement = select(*columns).where(
        Memory.id.in_(wanted),
        Memory.workspace_id == context.workspace_id,
        Memory.context_id == context.id,
        Memory.deleted_at.is_(None),
    )
    try:
        async with db.begin_nested():
            rows = (await db.execute(statement)).all()
    except Exception as exc:
        logger.warning("public_search_filter_unread", error_type=type(exc).__name__)
        return unique[:limit], None, len(results) - len(unique)
    live = {row.id: row for row in rows}
    filtered = [result for result in unique if ids[result.memory_id] in live]
    kept = filtered[:limit]
    doc_ids: tuple[str, ...] | None = None
    if publication is not None:
        served_ids: list[str] = []
        for result in kept:
            row = live[ids[result.memory_id]]
            served = bool(row.in_publication) and row.resource_doc_id is not None
            served_ids.append(str(row.resource_doc_id) if served else "")
        doc_ids = tuple(served_ids)
    return kept, doc_ids, len(results) - len(filtered)


# ============================================================================
# Public Search Endpoint
# ============================================================================


@router.post("/{context_id}/search", response_model=PublicSearchResponse)
async def public_search(
    context_id: UUID,
    request: PublicSearchRequest,
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
    visitor: VisitorHeaderSource = Depends(visitor_header_source),
):
    """Search public context with schema-aware response formatting.

    Issue #238: Public REST API for external systems (EC inventory, docs, etc.).
    Issue #626: Optional API-key attribution via ``Authorization: Bearer
    <kagura_…>``. A bound key gets a per-key rate-limit bucket and audit
    attribution; an unbound key (owner / workspace-scoped) is rejected so
    callers don't accidentally route their primary credentials through a
    public path. Anonymous access continues to work without any header.

    The route does not read the session (#2823): a request with a session
    cookie is served, counted and refused exactly like one without it.

    Authentication:
        - Public-bound API key (#626) — per-key rate limit, audit-attributed
        - Anonymous access, session cookie or not (rate limited to 50
          req/min per context, shared)

    No caller is reranked (#3035): ``use_rerank`` is accepted and ignored.

    Results list each row once and only the context's live rows (#3035), so a
    search can return fewer than ``limit`` results.

    A bound key's last-used time is committed once the key passes the context
    check (404 / 403 above), before the search runs, also when its own
    per-minute bucket or its plan then refuses the search.

    Owner statistics (#3035, while ``PUBLIC_SEARCH_TELEMETRY_ENABLED`` is on):
    a search made with a bound key of a context whose public MCP endpoint is
    serving is recorded as a usage event of that endpoint (route ``rest``,
    caller ``bound_key``). Such a key may send ``Kagura-Visitor: <value>``
    (1-64 characters of ``[A-Za-z0-9_-]``); the value is turned at once into a
    daily key and never stored as sent; a malformed or repeated header is
    ignored, never refused. Anonymous searches are never recorded and their
    ``Kagura-Visitor`` is never read.

    Rate Limits:
        - Anonymous access: 50 requests/minute per context (shared bucket)
        - Public-bound key: per-key bucket from workspace plan tier
          (``bound_public_calls_per_minute``)
        - Public usage budget (#2805, when MCP_PUBLIC_USAGE_BUDGET_ENABLED
          is on): an anonymous search of a context with a live public MCP
          publication is charged to the workspace's seven-day budget, shared
          with its public MCP endpoints (docs/public-usage-budget.md).
          Bound keys are never charged.

    Request:
        POST /api/v1/public/{context_id}/search
        Headers:
            Authorization: Bearer kagura_…    (optional; required only for
                                               attributed/bound-key access)
        Body:
        {
            "query": "商品名 価格",
            "limit": 10
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
        - 429: ``RATE-001`` with ``details.reason`` (#3035):
               ``anonymous_limit`` (the context's shared bucket),
               ``pre_auth_limit`` (requests with ``Authorization`` per
               context), ``key_limit`` (the bound key's own bucket) or
               ``key_not_provisioned`` (the plan gives bound keys no
               allowance); or the workspace's plan has no public API
               allowance (#1939), or ``usage_budget_exhausted`` (#2805,
               ``Retry-After`` 60–3600 s)
        - 503: ``temporarily_unavailable`` — on the budget-charged path, the
               budget store failed or the per-minute bucket could not be
               read (#2805, ``Retry-After: 60``); or the workspace cannot
               run searches for now (#3035, ``Retry-After: 300``, no reason
               given)
    """
    start_time = utcnow()
    # #3035: the clock of a recorded search's duration (the whole route).
    started = time.perf_counter()
    # #3035 (K8): the stage clock behind both result lines (no query text).
    timings = RecallTimings()

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
        **query_log_fields(request.query),  # #1721 (Directory 1.D): no query text
        limit=request.limit,
        has_bound_key=bound_key is not None,
        bound_key_id=bound_key.id if bound_key else None,
    )

    # 2. Get context and verify it's public. A soft-deleted context reads as
    # a missing one.
    context = await db.get(Context, context_id)
    if not context or context.deleted_at is not None:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        # When the binding row points to a context that the owner has since
        # flipped to is_public=false, we deny here exactly like anonymous —
        # the binding row stays so the owner can re-enable, but access is
        # blocked while the public flag is off.
        raise AuthorizationError("This context is not public")

    # #3035: ``verify_key`` wrote the bound key's throttled ``last_used_at`` in
    # this transaction; without a commit here its row lock would be held
    # through the whole search, and concurrent requests with the same key
    # would queue behind it. A key refused above (attribution, missing or
    # non-public context) is rolled back with the request and keeps its row.
    # ``expire_on_commit=False`` keeps the loaded context usable.
    if bound_key is not None:
        await db.commit()

    # 3. Rate limit dispatch: a public-bound key, or anonymous (#2823: a
    # session cookie does not change the principal on this route).
    # The Workspace fetch is deferred to after this gate so a flood of
    # 429-bound anonymous requests don't burn a DB lookup each. Bound-key
    # path needs the plan tier (so it fetches workspace inline); the
    # anonymous path fetches lazily below, only on requests that survive
    # the bucket.
    workspace: Workspace | None = None
    anonymous_bucket_counted = False
    bound_limit = 0
    # #3035: a bound-key search is recorded for the owner's statistics while
    # recording is on and its plan gives bound keys an allowance (limit 0 is a
    # bound key's plan pause, never recorded: no lookup, no header read).
    # Anonymous searches are never recorded and their header is never read.
    record: SearchRecord = NULL_RECORD
    if bound_key is not None:
        workspace = await db.get(Workspace, context.workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Context workspace not found",
            )
        plan = get_plan_tier(workspace.plan_name or "free")
        bound_limit = plan.bound_public_calls_per_minute
        if public_search_recording_enabled() and bound_limit > 0:
            record = await start_bound_record(
                db,
                context=context,
                started_at=start_time,
                clock_start=started,
                visitor=visitor,
            )
    # The record exists before this ``try``: every exit below offers it once,
    # from the ``finally``; a failure after the answer was built wins.
    try:
        if bound_key is not None:
            # The visitor key is computed first and the raw value dropped.
            record.resolve_visitor()
            await check_bound_key_rate_limit(bound_key.id, bound_limit)
        else:
            # Anonymous shared bucket. Workspace lookup is deferred to step 4
            # below: under abuse, a 429 returns BEFORE any Workspace SELECT.
            anonymous_bucket_counted = await check_public_search_rate_limit(context_id)

        # 4. Resolve workspace lazily if not already fetched above. Required
        # for the embedding-service caller user_id (search_user_id).
        if workspace is None:
            workspace = await db.get(Workspace, context.workspace_id)
            if workspace is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Context workspace not found",
                )
        # The context of a soft-deleted workspace reads as missing too (a bound
        # key of one was refused above: its owner holds no live workspace). For
        # an anonymous caller this is decided after the anonymous bucket, where
        # the Workspace row is first read, so past the bucket it answers 429.
        if workspace.deleted_at is not None:
            raise NotFoundException("Context", str(context_id))

        # #1939: anonymous serving pauses when the owner's plan has no public
        # allowance (it fell back to Free) — after the anonymous bucket, so a
        # flood is still refused before this lookup. The bound-key path is
        # already refused on such a plan (zero per-key bucket), so this gate only
        # has to cover anonymous callers.
        if bound_key is None:
            ensure_public_serving_allowed(workspace)

        # #1941: a workspace over its Free capacity pauses search, public reads
        # included. Placed after the rate buckets and on the already-loaded
        # Workspace, so a 429 flood costs no lock read. Order with the plan-pause
        # gate (#1939): plan pause (anonymous) first, then this capacity check.
        # Every caller of this route is an outsider (anonymous or bound key), so
        # it gets the redacted refusal.
        # #3035: a serving gate's refusal leaves no event (public MCP refuses
        # these in its transport, before any call record).
        with record.gate():
            await ensure_not_capacity_locked(db, workspace, outsider=True)
            # #2918: a slot-suspended workspace serves no public read (redacted).
            await ensure_slot_active(db, workspace, user_id=None, outsider=True)

        # #2805: an anonymous search of a context with a live public MCP
        # publication spends the workspace's public usage budget, like a public
        # tools/call. After the bucket, the plan pause and the capacity check;
        # before the search, so a search that fails afterwards stays counted.
        # Bound keys are never charged.
        if bound_key is None:
            await _charge_public_usage_budget(db, context, bucket_counted=anonymous_bucket_counted)

        # 5. Hoist usage-log attribution + caller id once so the success and
        # error paths below share one definition.
        log_user_id: str = str(bound_key.user_id) if bound_key is not None else "anonymous"
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

            # #3035: no caller is reranked (bound key, anonymous, session cookie or
            # not). An explicit False: None would follow the context's search
            # config and could rerank.
            degradation: dict[str, Any] = {}
            results = await search_service.hybrid_search(
                query=request.query,
                user_id=search_user_id,
                workspace_id=str(context.workspace_id),
                context_id=str(context_id),
                # #3035 (K4a): twice the limit, as public MCP fetches, so serving
                # each row once still fills ``limit`` in the common case.
                k=2 * request.limit,
                use_rerank=False,
                search_mode=request.search_mode,
                degradation=degradation,
                timings=timings,
            )

            schema = (
                await get_latest_schema(db, context.workspace_id, context.resource_id)
                if context.resource_id is not None
                else None
            )

            formatted_results = [
                PublicSearchResult(
                    # The hit id is the Qdrant point id; a resource point is not the
                    # row's id, the payload carries it (#2953).
                    memory_id=str(result.get("payload", {}).get("memory_id") or result["id"]),
                    content=result.get("payload", {}).get(
                        "summary", result.get("payload", {}).get("content", "")
                    ),
                    score=result["score"],
                    metadata=result.get("payload", {}),
                    highlighted=None,  # TODO: Implement highlighting in future
                )
                for result in results
            ]
            # #3035 (K4a): each row once, only the context's live rows, at most
            # ``limit``; ``count`` follows, so it can be below ``limit``.
            formatted_results, doc_ids, results_dropped = await _filter_results(
                db,
                context=context,
                results=formatted_results,
                limit=request.limit,
                publication=record.publication,
            )
            # #3035: the answer is known; a failure after this still wins.
            record.served(
                degraded=bool(degradation.get("degraded")),
                result_count=len(formatted_results),
                doc_ids=doc_ids,
            )

            # Bound path only: whether this search is recorded, and why its
            # visitor header was ignored (never the value).
            record_fields: dict[str, Any] = (
                {}
                if bound_key is None
                else {"recorded": record.live, "visitor_ignored": record.visitor_ignored}
            )
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
                results_dropped=results_dropped,
                **record_fields,
                has_schema=schema is not None,
                response_time_ms=response_time_ms,
                fusion=timings.fusion,
                **timings.log_fields(),
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
            # #3035 (K10a): the workspace cannot run searches for now. The answer
            # is the bare 503 with no reason or figure; the operator line names
            # the cause (its message carries the figure, so it is not logged).
            cannot_run = isinstance(e, EmbeddingSpendCapExceeded)
            response_time_ms = int((utcnow() - start_time).total_seconds() * 1000)
            await log_usage(
                db=db,
                user_id=log_user_id,
                endpoint=f"/api/v1/public/{context_id}/search",
                method="POST",
                status_code=503 if cannot_run else 500,
                response_time_ms=response_time_ms,
                **log_attribution,
            )

            if cannot_run:
                logger.error(
                    "public_search_failed",
                    context_id=context_id,
                    error_type=type(e).__name__,
                    reason="embedding_spend_cap",
                    fusion=timings.fusion,
                    **timings.log_fields(),
                )
                raise TemporarilyUnavailableError() from None
            logger.error(
                "public_search_failed",
                context_id=context_id,
                error=str(e),
                fusion=timings.fusion,
                **timings.log_fields(),
            )
            # Do not leak the raw exception message to the (potentially
            # anonymous) client — DB / provider / library errors can carry
            # column names, connection strings, file paths, etc. The full
            # message is in the logger.error above for operator triage.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal search error",
            ) from e
    except BaseException as exc:
        record.failed(exc)
        raise
    finally:
        record.emit()


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
        - 429: The workspace's plan has no public API allowance (#1939), or
               ``RATE-001`` with ``details.reason`` ``pre_auth_limit``
               (requests with ``Authorization`` per context, #3035)
    """
    # Issue #626: pre-auth rate limit + IDOR guard run before context
    # lookup, symmetry with /search. The pre-auth gate protects
    # ``verify_key`` from invalid-key DB-DoS amplification.
    if api_key is not None:
        await check_pre_auth_rate_limit(context_id)
    await _resolve_public_attribution(api_key, context_id, db)

    # Get context (a soft-deleted one reads as missing, as on /search)
    context = await db.get(Context, context_id)
    if not context or context.deleted_at is not None:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        raise AuthorizationError("This context is not public")

    # #1939: the context stays public, but serving pauses while the owner's
    # plan has no public allowance — same refusal as /search.
    workspace = await db.get(Workspace, context.workspace_id)
    if workspace is not None:
        if workspace.deleted_at is not None:
            raise NotFoundException("Context", str(context_id))
        ensure_public_serving_allowed(workspace)
        # #2918: nor while the workspace is slot-suspended (redacted, as /search).
        await ensure_slot_active(db, workspace, user_id=None, outsider=True)

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
