"""Embedding service for vector generation.

Uses workspace-scoped OpenAI API keys stored in the ExternalAPIKey table (#385):
the current workspace's enabled OpenAI key is shared by every member of that
workspace. `user_id` on the key is creator metadata only, not a visibility filter.

Issue #1: API keys are DB-managed, not in .env
Issue #84 Phase 2A: Redis caching with xxHash keys (50-80% API reduction, 4x space savings)
Issue #105: DB-first API key retrieval with environment variable fallback
Issue #385: workspace-keyed lookup (was user-keyed pre-#385)
"""

from __future__ import annotations

import json
import os
import unicodedata
from typing import TYPE_CHECKING, Literal

import xxhash
from openai import AsyncOpenAI
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import has_feature
from db.redis import get_cache, set_cache
from models.auth import ExternalAPIKey, Workspace
from utils.encryption import get_encryptor
from utils.exceptions import (
    ConfigurationError,
    EmbeddingSpendCapExceeded,
    NotFoundException,
    OpenAIError,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from services.embedding_spend_cap_service import EmbeddingSpendCapService

logger = get_logger(__name__)

# The credential tier ``_get_user_api_key`` resolved a key from. ``"workspace"``
# / ``"context"`` are BYOK rows in ``external_api_keys``; ``"env"`` is the
# platform ``OPENAI_API_KEY`` fallback. Maps directly to ``LLMCallLog.paid_by``
# (``"env"`` → ``"platform"``, otherwise → ``"byok"``) — see ``resolve_paid_by``.
KeySource = Literal["workspace", "context", "env"]


class EmbeddingService:
    """Service for generating embeddings using OpenAI or Ollama.

    Supports multiple providers via OpenAI-compatible API.
    User-specific API keys are retrieved from database (encrypted).
    """

    def __init__(
        self,
        db: AsyncSession,
        model: str | None = None,
        dimensions: int | None = None,
    ):
        """Initialize embedding service.

        Args:
            db: Database session (for retrieving user's API key)
            model: Embedding model name (default: from settings)
            dimensions: Vector dimensions (default: from settings or model registry)
        """
        from config.constants import EMBEDDING_MODEL_REGISTRY
        from config.settings import get_settings

        settings = get_settings()
        self.db = db
        self.model = model or settings.embedding_model
        # Look up dimensions from registry if not explicitly provided
        if dimensions is not None:
            self.dimensions = dimensions
        elif self.model in EMBEDDING_MODEL_REGISTRY:
            self.dimensions = EMBEDDING_MODEL_REGISTRY[self.model][0]
        else:
            self.dimensions = settings.embedding_dimensions
        # Determine provider from registry or settings
        if self.model in EMBEDDING_MODEL_REGISTRY:
            self.provider = EMBEDDING_MODEL_REGISTRY[self.model][1]
        else:
            self.provider = settings.embedding_provider
        self.ollama_base_url = settings.ollama_base_url
        self._ollama_verified = False
        # Issue #713: the credential tier the most recent ``_get_client`` OpenAI
        # call resolved (set in ``_get_client``). ``resolve_paid_by`` reads it to
        # derive ``paid_by`` without re-probing ``external_api_keys`` — the
        # service is request-scoped (one per recall, holding a per-request
        # session) so this state is never shared across concurrent requests.
        self._last_key_source: KeySource | None = None

    async def _get_user_api_key(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # Issue #146
        disallow_env_fallback: bool = False,
    ) -> tuple[str, KeySource]:
        """Resolve the OpenAI API key for the calling user's workspace context.

        Issue #105: DB-first lookup with environment variable fallback.
        Issue #146: workspace-scoped keys.
        Issue #385: the lookup is workspace-keyed, not user-keyed — any workspace
            member can use the owner-registered workspace API key. The `user_id`
            parameter is retained for audit logging only; it is NOT a visibility
            filter.

        Priority:
        1. Context-scoped key (context_id matches AND workspace_id matches)
        2. Workspace-scoped key (workspace_id matches AND context_id IS NULL)
        3. Environment variable (OPENAI_API_KEY) — development fallback only

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter (#385).
            context_id: Optional context UUID (#82: context-scoped keys take priority).
            workspace_id: Workspace UUID (#146); when omitted the DB lookup is skipped
                and only the env-var fallback can satisfy the request.
            disallow_env_fallback: Issue #708 loop 7. When True, skip the
                ``OPENAI_API_KEY`` env-var fallback at priority 3 and raise
                ``ConfigurationError`` instead. Set by the Option A shared-context
                read path so a TOCTOU race between the preflight ``has_byok_key``
                probe and this resolution cannot silently route to the uncapped
                platform key. Has no effect when a DB key is found.

        Returns:
            ``(api_key, source)`` where ``source`` is the credential tier the
            key came from — ``"context"`` / ``"workspace"`` for a BYOK row,
            ``"env"`` for the platform ``OPENAI_API_KEY`` fallback. Issue #713:
            callers derive ``LLMCallLog.paid_by`` from ``source`` (``"env"`` →
            ``"platform"``, else ``"byok"``) instead of re-running the BYOK
            probe via ``has_byok_key``.

        Raises:
            ConfigurationError: If neither a DB key nor an env var is available
                (or when ``disallow_env_fallback`` is True and no DB key was found).
        """
        from uuid import UUID

        from sqlalchemy import or_

        api_key_entry = None
        if workspace_id:
            workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
            conditions = [
                ExternalAPIKey.workspace_id == workspace_uuid,
                ExternalAPIKey.provider == "openai",
                ExternalAPIKey.enabled.is_(True),
            ]
            if context_id:
                context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
                # Context-scoped key wins; fall back to workspace-scoped (context_id IS NULL).
                conditions.append(
                    or_(
                        ExternalAPIKey.context_id == context_uuid,
                        ExternalAPIKey.context_id.is_(None),
                    )
                )
            else:
                conditions.append(ExternalAPIKey.context_id.is_(None))

            query = (
                select(ExternalAPIKey)
                .where(and_(*conditions))
                .order_by(ExternalAPIKey.context_id.desc().nulls_last())
                .limit(1)
            )
            result = await self.db.execute(query)
            api_key_entry = result.scalar_one_or_none()

        if api_key_entry:
            encryptor = get_encryptor()
            api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
            # The ORDER BY context_id DESC NULLS LAST in the query above means a
            # context-scoped row wins over a workspace-wide one; a non-NULL
            # ``context_id`` therefore identifies a context-scoped credential.
            source: KeySource = "context" if api_key_entry.context_id is not None else "workspace"
            logger.debug(
                "openai_api_key_from_db",
                user_id=user_id,
                context_id=context_id,
                workspace_id=workspace_id,
                source=source,
            )
            return api_key, source

        # Fallback to environment variable (development / env-only deployments),
        # gated by the Option A shared-context contract: when the caller has
        # explicitly required a BYOK key (disallow_env_fallback=True), do NOT
        # silently route to the platform key — that would defeat PR #711's
        # BYOK-only spend cap and bypass the H1 preflight guard via TOCTOU.
        #
        # #708 loop 8 (Copilot): when the disallow path fires we must NOT
        # fall through to the ``ConfigurationError`` below — it would
        # interpolate the source ``workspace_id`` into its message and the
        # MCP exception serializer would leak that workspace UUID to the
        # caller, defeating the H2 uniform-disclosure contract
        # (CWE-639 / OWASP A01). Raise ``NotFoundException("Context")``
        # instead so the recall handler maps it to the same uniform
        # ``context_not_found`` response shape as a regular deny.
        if disallow_env_fallback:
            logger.warning(
                "openai_api_key_env_fallback_disallowed",
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=context_id,
                reason="option_a_shared_context_read",
            )
            raise NotFoundException("Context")

        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            logger.debug(
                "openai_api_key_from_env",
                user_id=user_id,
            )
            return env_key, "env"

        if workspace_id:
            raise ConfigurationError(
                f"OpenAI API key not configured for workspace {workspace_id}. "
                "Configure a workspace OpenAI API key in settings, or set the "
                "OPENAI_API_KEY environment variable."
            )
        raise ConfigurationError(
            "OpenAI API key not configured: no workspace context was provided. "
            "Provide a workspace_id, configure a workspace OpenAI API key in "
            "settings, or set the OPENAI_API_KEY environment variable."
        )

    async def _prepare_spend_cap_gate(
        self,
        workspace_id: str | None,
        context_id: str | None = None,
        disallow_env_fallback: bool = False,
    ) -> tuple[EmbeddingSpendCapService | None, Workspace | None]:
        """Resolve the cap service + workspace and run the pre-call gate (#709, #1033).

        Returns ``(cap_svc, cap_workspace)`` when the call should be capped, or
        ``(None, None)`` to skip. Skip cases:

        - **Ollama** — no real provider cost; cap is irrelevant.
        - **Missing ``workspace_id``** — no caller scope to resolve a cap against.
        - **Workspace row missing** — disappeared between request entry and embed.
        - **No BYOK on a plan without ``managed_embeddings`` (Free / S)** — a
          no-BYOK call would fall back to the platform ``OPENAI_API_KEY`` env.
          By default Free / dev stays **uncapped** (#708 drain-attack carve-out:
          capping env-fallback would rate-limit the dev/demo workspaces that
          were never the threat model). Issue #1030: when
          ``embedding_platform_fallback_requires_managed_plan`` is enabled, this
          path instead raises ``ConfigurationError`` (Free = "BYOK required or
          self-host Ollama") — paid tiers carry ``managed_embeddings`` and never
          reach this branch.
        - **No BYOK while ``disallow_env_fallback`` is set** — the Option A
          shared-context read path forbids the env fallback, so this call will
          raise ``NotFoundException`` in ``_get_client`` rather than embed on the
          platform key. There is no platform spend to bound here, so skip (and
          let the real not-found error surface instead of a spurious 429).

        Otherwise the cap fires against the **single per-workspace per-tier
        counter**. Issue #1033 extends *coverage* of that one counter; it does
        NOT split it by payer:

        - **BYOK present** → capped exactly as before (#709), on any tier.
        - **No BYOK on a PAID tier (basic/pro)** → the embed runs on the
          platform's dime, so it is now capped too — bounding total workspace
          embedding volume against the same per-tier counter. Payer
          **attribution** is recorded separately in ``LLMCallLog.paid_by`` (see
          ``resolve_paid_by`` / ``cost_aggregation_service``), so the cap stays a
          pure volume guard rather than a per-payer budget. Prerequisite guard
          for #1030; inert until paid-tier traffic routes to the platform key,
          but lands first so the ceiling is in place.

        The workspace is loaded ONCE and reused for both ``check_cap_or_raise``
        and the post-call ``record_spend_from_tokens`` (no second SELECT). The
        load now also runs on the no-BYOK paid path (to read ``plan_name``);
        since the gate only runs on a cache MISS this is at most one extra
        indexed PK SELECT per unique text, not per call.

        Issue #708 loop 4: ``context_id`` mirrors ``has_byok_key``'s priority so
        the BYOK branch fires only when ``_get_user_api_key`` would actually
        select a BYOK row for THIS context — not for a key scoped to some OTHER
        context in the same workspace.
        """
        if not workspace_id or self.provider == "ollama":
            return None, None
        from services.embedding_spend_cap_service import EmbeddingSpendCapService

        has_byok = await self.has_byok_key(workspace_id, context_id=context_id)
        # No BYOK + env fallback forbidden → the call errors in ``_get_client``
        # rather than embedding on the platform key; there is nothing to cap.
        if not has_byok and disallow_env_fallback:
            return None, None

        cap_svc = EmbeddingSpendCapService(self.db)
        cap_workspace = await cap_svc.load_workspace(workspace_id)
        if cap_workspace is None:
            return None, None
        # No BYOK on a plan WITHOUT managed embeddings (Free / S) → the call
        # would fall back to the platform OPENAI_API_KEY env.
        if not has_byok and not has_feature(cap_workspace.plan_name, "managed_embeddings"):
            from config.settings import get_settings

            if get_settings().embedding_platform_fallback_requires_managed_plan:
                # Issue #1030: Free is "BYOK required or self-host Ollama". Deny
                # the platform fallback with a clear, actionable error rather
                # than silently embedding on the platform key. Paid tiers
                # (basic/pro) have managed_embeddings, so they never reach here.
                raise ConfigurationError(
                    "This workspace's plan does not include managed embeddings. "
                    "Add a BYOK OpenAI embedding key for the workspace, use a "
                    "self-hosted Ollama model, or upgrade to a paid plan."
                )
            # Restriction off (default): platform env-fallback stays uncapped —
            # the #708 drain-attack carve-out (Free has a $0.50/day drain guard,
            # not a platform budget). Capping it would throttle dev/demo flows.
            return None, None
        await cap_svc.check_cap_or_raise(cap_workspace)
        return cap_svc, cap_workspace

    async def resolve_paid_by(
        self,
        workspace_id: str | None,
        context_id: str | None = None,
    ) -> str:
        """Resolve the ``LLMCallLog.paid_by`` value for this embed call (#709).

        Returns ``"byok"`` when the workspace owns the credential used for
        the call, ``"platform"`` when the call would fall back to the env
        var (or Ollama / no workspace context). Callers must pass the
        returned value through to ``LLMCallLogWriter.record(paid_by=...)``
        — see the recall embed branch in ``SearchService`` for the
        canonical usage.

        Centralizing the resolution here keeps the ``"byok" if … else
        "platform"`` rule in one place; downstream writers don't need to
        know how key sourcing works.

        Issue #713: when a key has already been resolved on this instance
        (the recall hot path: ``embed_with_usage`` → ``_get_client`` →
        ``_get_user_api_key`` ran on a cache miss), derive ``paid_by`` from
        the recorded ``_last_key_source`` instead of re-querying
        ``external_api_keys``. ``_get_user_api_key``'s actual return is the
        ground truth for which key was used — strictly more accurate than a
        second ``has_byok_key`` probe — so this both removes a DB round-trip
        and closes the #708 loop-4 probe/lookup drift by construction. The
        ``has_byok_key`` fallback below covers only the off-hot-path case
        where ``resolve_paid_by`` is called without a preceding embed (no
        source resolved yet, e.g. Ollama or a standalone caller); recall
        never reaches it because ``search_service`` calls ``resolve_paid_by``
        only after a cache-miss embed recorded the source. ``context_id`` is
        still forwarded to that fallback so it mirrors ``_get_user_api_key``'s
        priority — without it, a workspace whose only BYOK is scoped to a
        different context would falsely log env-fallback calls as ``"byok"``.
        """
        from models.llm_call_log import LLM_CALL_LOG_PAID_BY_VALUES

        if self._last_key_source is not None:
            paid_by = "platform" if self._last_key_source == "env" else "byok"
        else:
            paid_by = (
                "byok"
                if await self.has_byok_key(workspace_id, context_id=context_id)
                else "platform"
            )
        assert paid_by in LLM_CALL_LOG_PAID_BY_VALUES
        return paid_by

    async def has_byok_key(
        self,
        workspace_id: str | None,
        context_id: str | None = None,
    ) -> bool:
        """Whether the workspace has an enabled BYOK key applicable to this context.

        Issue #709: used by writers (``search_service.py``) to set
        ``LLMCallLog.paid_by`` accurately — ``'byok'`` when the workspace
        owns the credential, ``'platform'`` when the call would fall back
        to ``OPENAI_API_KEY`` env. Returns ``False`` for Ollama and when
        no ``workspace_id`` is provided. Tolerates both UUID and string
        ``workspace_id`` inputs.

        Issue #708 (loop 1 + loop 7 fix): ``context_id`` mirrors the
        priority rules of ``_get_user_api_key`` exactly:

        - When ``context_id`` is supplied, the probe matches either a
          context-scoped key for the same context OR a workspace-wide
          key (``context_id IS NULL``).
        - When ``context_id`` is omitted, the probe matches ONLY
          workspace-wide keys (``context_id IS NULL``) — same as
          ``_get_user_api_key(..., context_id=None)``. Without this
          symmetric default, the probe would return True for a
          workspace whose only BYOK is scoped to some context X, while
          a no-context call to ``_get_user_api_key`` later would not
          select that key and fall back to env — re-introducing the
          accounting drift the context-aware probe is meant to close.

        This is a cheap existence probe (single indexed SELECT, no decrypt)
        and lives alongside ``_get_user_api_key`` to avoid duplicating its
        priority rules in callers.
        """
        from uuid import UUID

        from sqlalchemy import or_

        if not workspace_id or self.provider == "ollama":
            return False
        ws_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        conditions = [
            ExternalAPIKey.workspace_id == ws_uuid,
            ExternalAPIKey.provider == self.provider,
            ExternalAPIKey.enabled.is_(True),
        ]
        if context_id:
            ctx_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
            conditions.append(
                or_(
                    ExternalAPIKey.context_id == ctx_uuid,
                    ExternalAPIKey.context_id.is_(None),
                )
            )
        else:
            # Symmetric with ``_get_user_api_key``: with no context_id, only
            # workspace-wide keys count.
            conditions.append(ExternalAPIKey.context_id.is_(None))
        stmt = select(ExternalAPIKey.id).where(*conditions).limit(1)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def _get_client(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
        disallow_env_fallback: bool = False,
    ) -> AsyncOpenAI:
        """Get the appropriate OpenAI-compatible client for the configured provider.

        Args:
            user_id: User ID (for API key retrieval)
            context_id: Optional context ID
            workspace_id: Optional workspace ID
            disallow_env_fallback: Forwarded to ``_get_user_api_key``. Issue
                #708 loop 7: when True (Option A shared-context reads), no
                ``OPENAI_API_KEY`` env fallback is allowed at key lookup.

        Returns:
            AsyncOpenAI client configured for the provider
        """
        if self.provider == "ollama":
            # Verify Ollama is reachable on first use only
            if not self._ollama_verified:
                import httpx

                try:
                    async with httpx.AsyncClient(timeout=5.0) as http:
                        resp = await http.get(self.ollama_base_url)
                        if resp.status_code != 200:
                            raise ConfigurationError(
                                f"Ollama not responding at {self.ollama_base_url} (HTTP {resp.status_code})"
                            )
                except httpx.ConnectError as err:
                    raise ConfigurationError(
                        f"Cannot connect to Ollama at {self.ollama_base_url}. "
                        "Is Ollama running? Start with: ollama serve"
                    ) from err
                self._ollama_verified = True
            return AsyncOpenAI(
                base_url=f"{self.ollama_base_url}/v1",
                api_key="ollama",  # Ollama doesn't require a real key
            )
        # OpenAI (default)
        api_key, source = await self._get_user_api_key(
            user_id,
            context_id,
            workspace_id,
            disallow_env_fallback=disallow_env_fallback,
        )
        # Issue #713: remember which credential tier this call used so the
        # post-embed ``resolve_paid_by`` can derive ``paid_by`` without a
        # second ``external_api_keys`` SELECT.
        self._last_key_source = source
        return AsyncOpenAI(api_key=api_key)

    def _build_embedding_kwargs(self, input_data: str | list[str]) -> dict:
        """Build kwargs for OpenAI-compatible embeddings.create() call."""
        kwargs: dict = {"model": self.model, "input": input_data}
        # OpenAI supports dimensions param; Ollama infers from model
        if self.provider == "openai":
            kwargs["dimensions"] = self.dimensions
        return kwargs

    async def embed(
        self,
        text: str,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> list[float]:
        """Generate embedding for text with Redis caching.

        Thin wrapper around ``embed_with_usage()`` that discards the
        token count — kept for callers that don't need cost attribution
        (recall path, ``resource_indexer``). Sleep phases 1/2/reindex
        all use ``embed_with_usage`` directly post-#475 PR-1; the recall
        path is the next migration target (#475 PR-3, gated on the #474
        event-store schema). ``resource_indexer.py``'s migration is a
        separate concern tracked outside #475.

        Args:
            text: Text to embed (summary)
            user_id: User ID (to retrieve API key)
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            Embedding vector (dimensions depend on configured model)

        Raises:
            OpenAIError: If embedding generation fails
            ConfigurationError: If API key not configured
        """
        vector, _ = await self.embed_with_usage(
            text,
            user_id=user_id,
            context_id=context_id,
            workspace_id=workspace_id,
        )
        return vector

    async def embed_with_usage(
        self,
        text: str,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
        disallow_env_fallback: bool = False,
    ) -> tuple[list[float], int]:
        """Generate embedding and return token usage for cost tracking (#471).

        Like ``embed()`` but also returns the input token count from the
        provider's API response, so callers can attribute embedding cost
        per-(provider, model) via ``llm_pricing``. Cache hits return
        ``(vector, 0)`` because cached responses consume no API tokens.

        This is an additive companion to ``embed()`` rather than a
        breaking signature change — ``embed()`` keeps working for callers
        that don't track cost (recall path, ``resource_indexer``). All
        three sleep phases (edge_discovery, dedup_merge, reindex) use
        ``embed_with_usage`` directly post-#475 PR-1. The recall path
        migration is deferred to #475 PR-3 (blocked on the event-store
        schema in #474).

        Args:
            text: Text to embed.
            user_id: User ID for API key lookup.
            context_id: Optional context ID for scoped API key.
            workspace_id: Optional workspace ID for scoped API key.
            disallow_env_fallback: Issue #708 loop 7. Forwarded to
                ``_get_user_api_key`` — when True the call MUST use a BYOK
                key from the DB or fail; the ``OPENAI_API_KEY`` env-var
                fallback is disabled. Set by Option A shared-context reads
                so a TOCTOU race between the preflight ``has_byok_key``
                probe and this call cannot silently route to the platform
                key (which would bypass PR #711's BYOK-only spend cap).

        Returns:
            ``(vector, tokens_used)``. ``tokens_used`` is 0 for cache
            hits and for providers that don't expose ``response.usage``.

        Raises:
            OpenAIError: If embedding generation fails.
            ConfigurationError: If API key not configured.
        """
        # Issue #713: clear any source recorded by a prior embed on this
        # (reused) instance so ``resolve_paid_by`` can never read a stale tier.
        # A cache hit returns before ``_get_client`` runs, leaving it None — and
        # ``search_service`` skips ``resolve_paid_by`` on a hit (0 tokens) — so
        # the only value it ever reads is the one this call resolves below.
        self._last_key_source = None
        try:
            normalized_text = unicodedata.normalize("NFC", text)

            text_hash = xxhash.xxh64(normalized_text.encode()).hexdigest()[:16]
            cache_key = f"emb:{self.model}:{text_hash}"
            cached = await get_cache(cache_key)

            if cached:
                vector = json.loads(cached)
                logger.debug(
                    "embedding_cache_hit",
                    user_id=user_id,
                    text_length=len(text),
                    cache_key=cache_key[:16] + "...",
                )
                return vector, 0

            # Issue #709/#1033: embedding spend cap gate (BYOK + paid-tier platform).
            # #708 loop 4: thread ``context_id`` so the gate's BYOK probe mirrors
            # ``_get_user_api_key``; ``disallow_env_fallback`` so a no-BYOK Option A
            # read (which won't reach the platform key) isn't treated as platform-paid.
            cap_svc, cap_workspace = await self._prepare_spend_cap_gate(
                workspace_id,
                context_id=context_id,
                disallow_env_fallback=disallow_env_fallback,
            )

            client = await self._get_client(
                user_id,
                context_id,
                workspace_id,
                disallow_env_fallback=disallow_env_fallback,
            )
            response = await client.embeddings.create(
                **self._build_embedding_kwargs(normalized_text)
            )

            vector = response.data[0].embedding
            # Embeddings only have an "input" side — no completion tokens.
            # OpenAI returns ``usage.prompt_tokens`` (and the same value as
            # ``total_tokens``); Ollama returns 0 because it has no usage
            # accounting. Read defensively — older provider SDKs may omit
            # the usage object entirely.
            usage = getattr(response, "usage", None)
            tokens_used = 0
            if usage is not None:
                tokens_used = (
                    getattr(usage, "prompt_tokens", None)
                    or getattr(usage, "total_tokens", None)
                    or 0
                )

            await set_cache(cache_key, json.dumps(vector), ttl=86400)

            logger.debug(
                "embedding_generated",
                user_id=user_id,
                text_length=len(text),
                vector_dim=len(vector),
                tokens=tokens_used,
                cached=False,
            )

            # Issue #709: post-call spend record. Reuses ``cap_workspace``
            # from the pre-call gate so no second SELECT is issued.
            if cap_svc is not None and cap_workspace is not None and tokens_used > 0:
                await cap_svc.record_spend_from_tokens(
                    cap_workspace,
                    provider=self.provider,
                    model=self.model,
                    tokens=tokens_used,
                )

            return vector, tokens_used

        except ConfigurationError:
            raise

        except EmbeddingSpendCapExceeded:
            # Issue #709: do NOT remap the 429 cap-exceeded into a 500 OpenAIError.
            # The cap exception must propagate so the FastAPI exception handler
            # returns a structured QUOTA-002 / 429 response to the client.
            raise

        except Exception as e:
            logger.error("embedding_failed", error=str(e), user_id=user_id)
            raise OpenAIError(f"Embedding generation failed: {e}") from e

    async def embed_batch(
        self,
        texts: list[str],
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
        disallow_env_fallback: bool = False,
    ) -> list[list[float]]:
        """Generate embeddings for multiple texts with Redis caching.

        Args:
            texts: List of texts to embed
            user_id: User ID
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)
            disallow_env_fallback: Issue #708 loop 7. Forwarded to
                ``_get_user_api_key`` — no ``OPENAI_API_KEY`` env fallback
                when True. Used by Option A shared-context reads.

        Returns:
            List of embedding vectors

        Raises:
            OpenAIError: If embedding generation fails

        Note:
            Issue #84 Phase 2A: Checks cache for each text, only generates uncached ones
        """
        try:
            # Issue #84 Phase 2A: Check cache for each text
            # Issue #122: Normalize all texts first for consistent caching
            normalized_texts = [unicodedata.normalize("NFC", t) for t in texts]

            results: list[list[float] | None] = []
            uncached_indices: list[int] = []
            uncached_texts: list[str] = []

            for i, text in enumerate(normalized_texts):
                text_hash = xxhash.xxh64(text.encode()).hexdigest()[:16]
                cache_key = f"emb:{self.model}:{text_hash}"
                cached = await get_cache(cache_key)

                if cached:
                    results.append(json.loads(cached))
                else:
                    results.append(None)
                    uncached_indices.append(i)
                    uncached_texts.append(text)

            cache_hit_rate = (len(texts) - len(uncached_texts)) / len(texts) * 100 if texts else 0

            logger.debug(
                "batch_embedding_cache_check",
                user_id=user_id,
                total=len(texts),
                cached=len(texts) - len(uncached_texts),
                uncached=len(uncached_texts),
                hit_rate=f"{cache_hit_rate:.1f}%",
            )

            # Generate embeddings only for uncached texts
            if uncached_texts:
                # Issue #709/#1033: cap gate covers the whole batch — single
                # check before the bulk API call (BYOK + paid-tier platform).
                # #708 loop 4: thread ``context_id`` so the gate's BYOK probe
                # mirrors ``_get_user_api_key``; ``disallow_env_fallback`` so a
                # no-BYOK Option A read isn't treated as platform-paid.
                cap_svc, cap_workspace = await self._prepare_spend_cap_gate(
                    workspace_id,
                    context_id=context_id,
                    disallow_env_fallback=disallow_env_fallback,
                )

                client = await self._get_client(
                    user_id,
                    context_id,
                    workspace_id,
                    disallow_env_fallback=disallow_env_fallback,
                )
                response = await client.embeddings.create(
                    **self._build_embedding_kwargs(uncached_texts)
                )

                # Cache new embeddings and fill results
                for i, (idx, item) in enumerate(zip(uncached_indices, response.data, strict=False)):
                    vector = item.embedding
                    results[idx] = vector

                    # Cache for 24 hours (use normalized text from uncached_texts)
                    text_hash = xxhash.xxh64(uncached_texts[i].encode()).hexdigest()[:16]
                    cache_key = f"emb:{self.model}:{text_hash}"
                    await set_cache(cache_key, json.dumps(vector), ttl=86400)

                logger.debug(
                    "batch_embedding_generated",
                    user_id=user_id,
                    generated=len(uncached_texts),
                )

                # Issue #709: post-call record (one batch = one usage row).
                if cap_svc is not None and cap_workspace is not None:
                    usage = getattr(response, "usage", None)
                    batch_tokens = 0
                    if usage is not None:
                        batch_tokens = (
                            getattr(usage, "prompt_tokens", None)
                            or getattr(usage, "total_tokens", None)
                            or 0
                        )
                    if batch_tokens > 0:
                        await cap_svc.record_spend_from_tokens(
                            cap_workspace,
                            provider=self.provider,
                            model=self.model,
                            tokens=batch_tokens,
                        )

            # Return all vectors (cached + newly generated)
            return [v for v in results if v is not None]

        except ConfigurationError:
            # Issue #1030: a missing key / managed-plan denial from the gate or
            # _get_client must propagate unchanged (CFG-001), not be masked as a
            # generic OpenAIError — mirrors embed_with_usage.
            raise

        except EmbeddingSpendCapExceeded:
            # Issue #709: propagate 429 cap-exceeded to the FastAPI exception
            # handler rather than wrapping in a generic 500 OpenAIError.
            raise

        except Exception as e:
            logger.error("batch_embedding_failed", error=str(e), user_id=user_id)
            raise OpenAIError(f"Batch embedding failed: {e}") from e
