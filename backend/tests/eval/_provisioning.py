"""Shared eval-workspace provisioning for the live eval runners.

v0.42 review #19: ``runner.py``, ``placebo_runner.py`` and ``freeze_tau.py`` all
create an isolated eval Workspace + Context + WorkspaceMember and stamp the
per-context ContextSearchConfig from settings the same way. This is the single
implementation; the runners call it, then ingest / measure / teardown.

The raw ORM Context bypasses ContextService.create_context (which normally
stamps the per-context embedding config and ensures the model-specific Qdrant
collection). Without an explicit ContextSearchConfig row the lazy create_or_get
defaults to text-embedding-3-small/512 and ingest routes to a collection that
does not exist on a non-default embedding rig (e.g. qwen3-embedding:0.6b/1024) —
so we stamp the config exactly like the production create path. The
WorkspaceMember row is required because remember()/recall() resolve the context
via PermissionService, which checks workspace_members (owner_user_id alone is
NOT a membership).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from auth.workspace_roles import WorkspaceRole
from config.constants import EMBEDDING_MODEL_REGISTRY
from config.settings import get_settings
from models.auth import Context, Workspace, WorkspaceMember
from models.config import ContextSearchConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def provision_eval_context(
    db: AsyncSession, *, sleep_mode: str | None = None, embedding_model: str | None = None
) -> tuple[str, Workspace, Context, str, int]:
    """Create + commit an isolated eval workspace/context and return handles.

    Args:
        db: Async session (from the runner's ``async for db in get_db()``).
        sleep_mode: Optional ``Context.sleep_mode`` (e.g. ``"edges_only"`` for
            the placebo edge-graph path); left at the model default when None.
        embedding_model: Optional explicit embedding model to stamp (Day-4
            factorial arm, #1175). Must be a key of ``EMBEDDING_MODEL_REGISTRY``
            (callers validate before the stack access). When None, the model is
            derived from settings — byte-identical to the pre-Day-4 behavior.

    Returns:
        ``(owner_id, workspace, context, embedding_model, embedding_dimensions)``.
    """
    owner = f"eval_{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"eval-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=10_000_000,
        weekly_api_limit=50_000_000,
    )
    ctx_kwargs = {
        "id": uuid4(),
        "workspace_id": ws.id,
        "name": f"eval-ctx-{uuid4().hex[:8]}",
        "created_by": owner,
        "is_private": False,
    }
    if sleep_mode is not None:
        ctx_kwargs["sleep_mode"] = sleep_mode
    ctx = Context(**ctx_kwargs)

    db.add(ws)
    await db.flush()
    db.add(ctx)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))

    # An explicit embedding_model (Day-4 factorial arm) overrides the
    # settings-derived stamp entirely; None keeps the settings-derived behavior.
    if embedding_model is not None:
        emb_model = embedding_model
        emb_dims = EMBEDDING_MODEL_REGISTRY[emb_model][0]
    else:
        settings = get_settings()
        emb_model = settings.embedding_model
        emb_dims = EMBEDDING_MODEL_REGISTRY.get(emb_model, (settings.embedding_dimensions, ""))[0]
    db.add(
        ContextSearchConfig(
            context_id=ctx.id,
            semantic_weight=0.6,
            fetch_factor=3,
            use_rerank=False,
            reranker_provider="self_hosted",
            reranker_model="qwen3-reranker-4b",
            embedding_model=emb_model,
            embedding_dimensions=emb_dims,
        )
    )
    await db.flush()
    await db.commit()
    return owner, ws, ctx, emb_model, emb_dims
