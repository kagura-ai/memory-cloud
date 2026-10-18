"""Repository for Context Search Configuration.

This module provides database access for context-level search and reranker settings.
Issue #130: Context-scoped Search & Reranker Settings UI
Issue #1220: Per-context router calibration store (stage 4)
"""

from datetime import datetime  # noqa: TC003 - runtime annotation in upsert()
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings, get_settings
from models.config import (
    ROUTER_CALIBRATION_SOURCE_FROZEN,
    ContextSearchConfig,
    RouterCalibration,
)
from models.schemas import ContextSearchConfigUpdate
from utils.logger import get_logger

logger = get_logger(__name__)


def search_config_defaults(settings: Settings) -> dict[str, Any]:
    """Reranker values for a new (or reset) context search config (#1572).

    The one place the deployment default (``DEFAULT_USE_RERANK`` /
    ``DEFAULT_RERANKER_PROVIDER`` / ``DEFAULT_RERANKER_MODEL``) becomes row
    values. Shared by :func:`new_search_config_defaults` (a new context's row:
    ``ContextService.create_context``, ``setup_resource``, ``/system/info``),
    :meth:`ContextSearchConfigRepository.create_or_get`,
    :meth:`ContextSearchConfigRepository.reset_to_default`, the MCP
    ``update_search_config`` fallbacks and the legacy-row
    materialisation in ``embedding_migration_service.switch_context_embedding``.
    Admin context recovery (``api/routes/admin.py``) deliberately does not use
    it: a recovered row pins the non-ranking-modifying values (#1207). The ORM
    column defaults stay as the static safety net for raw inserts; existing
    rows are never rewritten here (see ``cli/apply_rerank_defaults.py``).

    Args:
        settings: Application settings — passed in so callers and tests decide
            which instance is read.

    Returns:
        ``{"use_rerank", "reranker_provider", "reranker_model"}`` ready to
        splat into ``ContextSearchConfig(...)`` or ``ContextSearchConfigUpdate(...)``.
        With the DEFAULT_* env unset this is ``False / "voyage" / "rerank-2"``.
    """
    # Lazy: reranker_service imports this module at import time.
    from services.reranker_service import default_reranker_model_for

    provider = settings.default_reranker_provider
    return {
        "use_rerank": settings.default_use_rerank,
        "reranker_provider": provider,
        "reranker_model": settings.default_reranker_model
        or default_reranker_model_for(provider, settings),
    }


def new_search_config_defaults(settings: Settings) -> dict[str, Any]:
    """Row values for a NEW context search config (#2015 D8).

    :func:`search_config_defaults` plus ``content_chunks_enabled`` from
    ``DEFAULT_CONTENT_CHUNKS_ENABLED``. Only where a NEW context is created —
    ``ContextService.create_context``, ``setup_resource`` — and in
    ``/system/info``. Never in :meth:`ContextSearchConfigRepository.reset_to_default`
    (a reset would let an editor turn chunks on, past the owner rule),
    :meth:`ContextSearchConfigRepository.create_or_get` (a legacy context's
    row, materialised by any reader), ``cli/apply_rerank_defaults.py`` or the
    migration's legacy-row materialisation: those rewrite or recreate an
    existing context's row, and turning chunks on there sends its content to
    the provider without its owner's choice.
    """
    return {
        **search_config_defaults(settings),
        "content_chunks_enabled": settings.default_content_chunks_enabled,
    }


def new_search_config_row(
    settings: Settings, *, content_chunks_enabled: bool | None = None
) -> dict[str, Any]:
    """:func:`new_search_config_defaults` plus ``content_chunks_enabled_at`` (#2103).

    What ``ContextSearchConfig(...)`` is built with for a NEW context. A row
    created with the flag on records the enable, like turning it on through
    :meth:`ContextSearchConfigRepository.update` does, so the plan's cap
    ranks it among the workspace's other contexts. Not in ``/system/info``
    (that serves :func:`new_search_config_defaults`).

    ``content_chunks_enabled`` overrides the deployment default: the creation
    paths pass ``services.content_chunk_policy.new_context_content_chunks``,
    which keeps it off where the plan gate would refuse turning it on.
    """
    defaults = new_search_config_defaults(settings)
    if content_chunks_enabled is not None:
        defaults["content_chunks_enabled"] = content_chunks_enabled
    return {
        **defaults,
        "content_chunks_enabled_at": (
            func.clock_timestamp() if defaults["content_chunks_enabled"] else None
        ),
    }


class ContextSearchConfigRepository:
    """Repository for managing context search configurations.

    Provides CRUD operations for context-specific hybrid search and reranker settings.
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository with database session.

        Args:
            db: Async SQLAlchemy session
        """
        self.db = db

    async def get_by_context(self, context_id: UUID) -> ContextSearchConfig | None:
        """Get search configuration for a context.

        Args:
            context_id: Context UUID

        Returns:
            ContextSearchConfig if exists, None otherwise
        """
        result = await self.db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
        )
        config = result.scalar_one_or_none()

        if config:
            logger.debug(
                "config_retrieved",
                context_id=str(context_id),
                semantic_weight=float(config.semantic_weight),
                fetch_factor=config.fetch_factor,
            )
        else:
            logger.debug("config_not_found", context_id=str(context_id))

        return config

    async def create_or_get(self, context_id: UUID) -> ContextSearchConfig:
        """Get existing config or create default if not exists.

        This method is idempotent - it will not create duplicates.

        Args:
            context_id: Context UUID

        Returns:
            ContextSearchConfig (existing or newly created)
        """
        # Try to get existing config
        config = await self.get_by_context(context_id)
        if config:
            return config

        # Create default config. The reranker fields are passed explicitly so
        # the lazily materialized row (a recall on a row-less legacy context)
        # gets the same deployment default as create_context (#1572). Not the
        # content chunk default (#2015 D8): this is an existing context, its
        # memories already written, and any reader materialises the row —
        # turning chunks on stays its owner's choice.
        logger.info("config_creating_default", context_id=str(context_id))
        config = ContextSearchConfig(
            context_id=context_id, **search_config_defaults(get_settings())
        )
        self.db.add(config)
        await self.db.commit()
        await self.db.refresh(config)

        logger.info(
            "config_created",
            context_id=str(context_id),
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
        )

        return config

    async def update(
        self,
        context_id: UUID,
        update_data: ContextSearchConfigUpdate,
    ) -> ContextSearchConfig:
        """Update search configuration for a context.

        Args:
            context_id: Context UUID
            update_data: Update request data

        Returns:
            Updated ContextSearchConfig

        Raises:
            ValueError: If config not found for context
        """
        config = await self.get_by_context(context_id)
        if not config:
            error_msg = f"Config not found for context {context_id}"
            logger.error("config_update_failed", context_id=str(context_id), error=error_msg)
            raise ValueError(error_msg)

        # Update fields
        backfill_queued: int | None = None
        update_dict = update_data.model_dump(exclude_unset=True)
        if "content_chunks_enabled" in update_dict:
            # The flag as committed, under the row lock: the caller loaded this
            # row before it took the cap's lock, and the identity map would
            # keep that stale value. A second enable of the same context
            # (a double click, a retry) then sees "already on" and keeps the
            # first enable's stamp, purge and backfill instead of re-ranking
            # the context and doing them again (v0.113.0 review chunks F2).
            await self.db.refresh(
                config,
                ["content_chunks_enabled", "content_chunks_enabled_at"],
                with_for_update=True,
            )
        chunks_were_on = getattr(config, "content_chunks_enabled", False) is True
        for key, value in update_dict.items():
            setattr(config, key, value)

        # #2015 invariant: a context with content_chunks_enabled off has no
        # chunk points, and recall only reads chunk points while it is on.
        # Any flag change purges them BEFORE the commit — off: if the purge
        # fails the update is rolled back and the flag stays on; on: chunks a
        # racing writer left while it was off never become visible. Turning
        # it off purges again AFTER the commit, for a writer that read "on"
        # between the first purge and the commit (the writer also re-reads
        # the flag right before its upsert). Turning it on also queues the
        # context's existing long memories for chunking (D4), in the same
        # transaction as the flag.
        chunks_now_on = getattr(config, "content_chunks_enabled", False) is True
        flag_changed = chunks_were_on != chunks_now_on
        if flag_changed:
            # #2103: the enable's rank under the plan's cap, committed with the
            # flag and its backfill (a failed enable rolls all three back).
            # clock_timestamp(), not now(): now() is the transaction's start,
            # and a request that waited on the cap's lock behind an earlier
            # enable would otherwise rank before it.
            config.content_chunks_enabled_at = func.clock_timestamp() if chunks_now_on else None
            try:
                await self._purge_chunk_points(context_id, config)
                if chunks_now_on:
                    backfill_queued = await self._queue_content_chunk_backfill(context_id)
            except Exception:
                await self.db.rollback()
                raise

        await self.db.commit()
        await self.db.refresh(config)
        if backfill_queued is not None:
            logger.info(
                "content_chunks_backfill_queued", context_id=str(context_id), count=backfill_queued
            )
        if flag_changed and not chunks_now_on:
            try:
                await self._purge_chunk_points(context_id, config)
            except Exception as exc:  # noqa: BLE001 — flag is committed off
                # Leftovers stay invisible (recall reads chunks only while the
                # flag is on) and are purged when it is turned on again.
                logger.warning(
                    "content_chunks_post_commit_purge_failed",
                    context_id=str(context_id),
                    error_class=type(exc).__name__,
                )
        logger.info(
            "config_updated",
            context_id=str(context_id),
            updated_fields=list(update_dict.keys()),
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
            use_rerank=config.use_rerank,
        )

        return config

    async def _queue_content_chunk_backfill(self, context_id: UUID) -> int:
        """Re-queue the context's existing long memories for chunking (#2015 D4).

        One UPDATE, in the caller's transaction: every live ``success`` row of
        the context whose content is long enough to split goes back to
        ``pending``, and the embedding sweep rebuilds it — summary point and
        chunk points — under the usual claim and retry. The row keeps its
        summary point meanwhile, so it stays searchable. Only ``success`` rows:
        a ``processing`` row is a run in flight (it writes its chunks itself
        now that the flag is on), ``pending`` / ``failed`` rows are picked up
        anyway. Neither ``embedding_attempted_at`` (the pipeline's clock) nor
        ``updated_at`` (an edit's cue) is written. Resource rows
        (``resource_id``) are left out: they never get chunks.

        Returns:
            How many rows were queued (0 on a store without chunk points).
        """
        from db.qdrant import chunk_points_supported
        from models.memory import Memory
        from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS

        if not chunk_points_supported():
            return 0
        result = await self.db.execute(
            update(Memory)
            .where(
                Memory.context_id == context_id,
                Memory.deleted_at.is_(None),
                Memory.embedding_status == "success",
                func.char_length(Memory.content) >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
                Memory.resource_id.is_(None),
            )
            .values(embedding_status="pending")
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def content_chunk_backfill_pending(self, context_id: UUID) -> int:
        """How many of the context's long memories still wait for their chunks.

        The live long non-resource rows not yet ``success`` — what a backfill
        queued and the sweep has not drained, plus new writes in flight
        (#2015 D4). Served on the search-config response for the UI.
        """
        from models.memory import Memory
        from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS

        result = await self.db.execute(
            select(func.count())
            .select_from(Memory)
            .where(
                Memory.context_id == context_id,
                Memory.deleted_at.is_(None),
                Memory.embedding_status != "success",
                func.char_length(Memory.content) >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
                Memory.resource_id.is_(None),
            )
        )
        return int(result.scalar_one() or 0)

    async def _purge_chunk_points(self, context_id: UUID, config: ContextSearchConfig) -> None:
        """Delete every content chunk point of a context (#2015), in every collection.

        Not only the routed one: an embedding migration keeps the context's
        points in its source collection until ``--purge`` and writes chunks
        into its target collection from ``--reembed`` on, and the invariant
        has to hold in both. Only the routed collection's purge is required
        (recall reads chunks from it alone); a failure elsewhere is logged by
        ``purge_context_chunk_points_everywhere`` and does not block the
        flag change. Turning the flag off retries every collection after the
        commit.
        """
        from db.qdrant import get_collection_name, purge_context_chunk_points_everywhere
        from models.auth import Context

        workspace_id = (
            await self.db.execute(select(Context.workspace_id).where(Context.id == context_id))
        ).scalar_one_or_none()
        if workspace_id is None:
            return
        failed = await purge_context_chunk_points_everywhere(
            str(workspace_id),
            str(context_id),
            routed_collection=get_collection_name(
                config.embedding_model, config.embedding_dimensions
            ),
        )
        logger.info("content_chunks_purged", context_id=str(context_id), failed=len(failed))

    async def reset_to_default(
        self, context_id: UUID, *, reset_content_chunks: bool = True
    ) -> ContextSearchConfig:
        """Reset configuration to default values.

        Args:
            context_id: Context UUID
            reset_content_chunks: Also turn ``content_chunks_enabled`` off
                (#2015). The REST route passes ``False`` when the flag is
                already off, so a reset by an editor never writes the flag —
                changing it takes the context owner.

        Returns:
            Reset ContextSearchConfig

        Raises:
            ValueError: If config not found for context
        """
        logger.info("config_resetting", context_id=str(context_id))

        # #2015: chunks off (update purges the context's chunk points) — only
        # when asked, so the flag is otherwise left out of the update.
        chunk_reset: dict[str, bool] = (
            {"content_chunks_enabled": False} if reset_content_chunks else {}
        )
        defaults = ContextSearchConfigUpdate(
            semantic_weight=0.6,
            bm25_weight=0.4,
            fetch_factor=3,
            # #1572: "reset" means the DEPLOYMENT default — the same reranker
            # values create_context writes — not the code default.
            **search_config_defaults(get_settings()),
            # #1207: reset converges reinforce to the documented defaults too.
            # These must be passed explicitly — update() applies
            # exclude_unset, so omitting them would leave stored values
            # behind and "reset to defaults" would not mean defaults.
            reinforce_enabled=True,
            reinforce_max_boost=0.15,
            reinforce_require_host_arbitration=False,
            **chunk_reset,
        )

        config = await self.update(context_id, defaults)

        logger.info("config_reset_complete", context_id=str(context_id))
        return config

    async def delete(self, context_id: UUID) -> bool:
        """Delete search configuration for a context.

        Note: In normal operation, configs are auto-deleted via CASCADE when context is deleted.
        This method is provided for manual cleanup if needed.

        Args:
            context_id: Context UUID

        Returns:
            True if deleted, False if not found
        """
        config = await self.get_by_context(context_id)
        if not config:
            logger.debug("config_delete_not_found", context_id=str(context_id))
            return False

        await self.db.delete(config)
        await self.db.commit()

        logger.info("config_deleted", context_id=str(context_id))
        return True


class RouterCalibrationRepository:
    """Repository for per-bucket router arm performance (#1220 stage 4).

    Rows with ``context_id IS NULL`` are the fleet defaults (frozen-corpus
    gate runs); per-context rows let managed-cloud tuning diverge without
    touching the self-host defaults.
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository with database session.

        Args:
            db: Async SQLAlchemy session
        """
        self.db = db

    async def upsert(
        self,
        *,
        bucket: str,
        arm: str,
        p_at_5: float,
        mrr_at_10: float,
        n_queries: int,
        sampled_at: datetime,
        context_id: UUID | None = None,
        source: str = ROUTER_CALIBRATION_SOURCE_FROZEN,
    ) -> RouterCalibration:
        """Insert or refresh one (scope, bucket, arm, source) measurement.

        ON CONFLICT targets the partial-unique index matching the scope
        (global vs per-context), so re-running a gate refreshes the row
        instead of failing or duplicating.

        Returns:
            The stored RouterCalibration row.
        """
        stmt = (
            pg_insert(RouterCalibration)
            .values(
                context_id=context_id,
                bucket=bucket,
                arm=arm,
                p_at_5=p_at_5,
                mrr_at_10=mrr_at_10,
                n_queries=n_queries,
                source=source,
                sampled_at=sampled_at,
            )
            .on_conflict_do_update(
                index_elements=(
                    ["bucket", "arm", "source"]
                    if context_id is None
                    else ["context_id", "bucket", "arm", "source"]
                ),
                index_where=text(
                    "context_id IS NULL" if context_id is None else "context_id IS NOT NULL"
                ),
                set_={
                    "p_at_5": p_at_5,
                    "mrr_at_10": mrr_at_10,
                    "n_queries": n_queries,
                    "sampled_at": sampled_at,
                },
            )
            .returning(RouterCalibration)
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one()
        logger.debug(
            "router_calibration_upserted",
            context_id=str(context_id) if context_id else None,
            bucket=bucket,
            arm=arm,
            source=source,
        )
        return row

    async def get_for_context(self, context_id: UUID | None) -> list[RouterCalibration]:
        """Calibration rows for a context, falling back to the fleet defaults.

        Returns the context's own rows when any exist, else the global
        (``context_id IS NULL``) rows. Never mixes scopes — a partially
        calibrated context should read as its own coherent measurement set.
        """
        if context_id is not None:
            result = await self.db.execute(
                select(RouterCalibration).where(RouterCalibration.context_id == context_id)
            )
            rows = list(result.scalars().all())
            if rows:
                return rows
        result = await self.db.execute(
            select(RouterCalibration).where(RouterCalibration.context_id.is_(None))
        )
        return list(result.scalars().all())
