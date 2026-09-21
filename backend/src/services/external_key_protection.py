"""When a stored external API key may not be deleted or disabled (#1613).

``PROTECTED_KEYS`` (#149) used to be the whole rule: ``OPENAI_API_KEY`` was
undeletable and OpenAI keys could not be disabled, because embeddings always
ran on that key. That stopped being true with ``self_hosted`` embeddings, the
managed LLM lane (#1569) and ``ENABLE_BYOK=false`` /
``RESOLVE_STORED_BYOK_KEYS=false`` (#1167, #1569): on such a deployment the
stored key is never read, yet its owner could not remove their own credential.

``PROTECTED_KEYS`` is now only the *candidate* set. A candidate is protected
while something would break without it — all of:

1. BYOK provisioning is on (``ENABLE_BYOK``) and the services still resolve
   stored keys (``RESOLVE_STORED_BYOK_KEYS``). With either off the owner can
   no longer replace the key, so withdrawing it must stay possible.
2. OpenAI embeddings are in use: the deployment's ``EMBEDDING_PROVIDER`` is
   ``openai``, or the workspace routes at least one live context to an OpenAI
   embedding model.

One predicate serves ``DELETE /external-keys/{key_name}``, the disable guard
and the ``is_protected`` flag of ``GET /external-keys``, so the UI's "Required"
badge and the refusals cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.plan_tiers import PROTECTED_KEYS
from config.settings import Settings
from models.auth import Context
from models.config import ContextSearchConfig

# The embedding provider whose credential the candidate keys hold.
_OPENAI = "openai"


@dataclass(frozen=True)
class KeyProtection:
    """Whether a key is protected, and why — the text a refusal shows."""

    protected: bool
    reason: str | None = None


_UNPROTECTED = KeyProtection(protected=False)


def embedding_provider_of(model: str, settings: Settings) -> str:
    """The provider that serves ``model``.

    Same rule as ``EmbeddingService.__init__``: the registry decides, and a
    model it does not know falls back to the deployment's
    ``EMBEDDING_PROVIDER``.
    """
    entry = EMBEDDING_MODEL_REGISTRY.get(model)
    return entry[1] if entry else settings.embedding_provider


def is_protection_candidate(key_name: str, settings: Settings) -> bool:
    """The half of the rule that needs no database.

    False means :func:`is_key_protected` is False whatever the workspace
    routes to, so callers can skip the context query.
    """
    return key_name in PROTECTED_KEYS and settings.enable_byok and settings.resolve_stored_byok_keys


def is_key_protected(
    key_name: str,
    settings: Settings,
    *,
    workspace_routes_to_openai_embeddings: bool,
) -> bool:
    """Whether ``key_name`` must not be deleted or disabled right now.

    Args:
        key_name: The stored key's name (``ExternalAPIKey.key_name``).
        settings: The deployment settings.
        workspace_routes_to_openai_embeddings: Whether the key's workspace has
            a live context on an OpenAI embedding model
            (:func:`count_openai_routed_contexts` ``> 0``).

    Returns:
        True only for a ``PROTECTED_KEYS`` candidate on a deployment that
        still resolves stored keys and has OpenAI embeddings in use.
    """
    if not is_protection_candidate(key_name, settings):
        return False
    return settings.embedding_provider == _OPENAI or workspace_routes_to_openai_embeddings


async def count_openai_routed_contexts(
    db: AsyncSession,
    workspace_id: UUID,
    settings: Settings,
) -> int:
    """How many live contexts of the workspace embed with an OpenAI model.

    Soft-deleted contexts do not count. A context without a
    ``ContextSearchConfig`` row embeds with the deployment default
    (``settings.embedding_model``), like ``resolve_context_routing``'s
    fallback to the caller's default ``EmbeddingService``.
    """
    result = await db.execute(
        select(ContextSearchConfig.embedding_model, func.count(Context.id))
        .select_from(Context)
        .outerjoin(ContextSearchConfig, ContextSearchConfig.context_id == Context.id)
        .where(Context.workspace_id == workspace_id, Context.deleted_at.is_(None))
        .group_by(ContextSearchConfig.embedding_model)
    )
    return sum(
        count
        for model, count in result.all()
        if embedding_provider_of(model or settings.embedding_model, settings) == _OPENAI
    )


async def evaluate_key_protection(
    db: AsyncSession,
    *,
    key_name: str,
    workspace_id: UUID,
    settings: Settings,
) -> KeyProtection:
    """Resolve :func:`is_key_protected` for one stored key, with its reason.

    Queries the workspace's contexts only when the answer depends on them: not
    for a non-candidate key, not with BYOK off, and not when the deployment
    itself embeds with OpenAI.
    """
    if not is_protection_candidate(key_name, settings):
        return _UNPROTECTED
    deployment_uses_openai = settings.embedding_provider == _OPENAI
    count = (
        0
        if deployment_uses_openai
        else await count_openai_routed_contexts(db, workspace_id, settings)
    )
    if not is_key_protected(key_name, settings, workspace_routes_to_openai_embeddings=count > 0):
        return _UNPROTECTED
    if deployment_uses_openai:
        in_use_by = "this deployment (EMBEDDING_PROVIDER=openai)"
    else:
        in_use_by = f"{count} context{'' if count == 1 else 's'} of this workspace"
    return KeyProtection(protected=True, reason=f"OpenAI embeddings are in use by {in_use_by}.")
