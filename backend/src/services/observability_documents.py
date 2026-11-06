"""Resolve opaque document keys to what the public endpoint already shows (#2679).

A ``doc_key`` (#2678) is an HMAC of ``(resource_pk, resource doc id)``; it
cannot be inverted. ``public_mcp_doc_keys`` (written with the usage events)
maps it back to the document id of that one resource. A key resolves only
when the resource's publication is **currently serving**
(``load_serving_endpoint``) and a memory of that document passes
``scope.public_rows`` — the same rows anonymous ``recall`` returns. Then the
answer is the document's title (its summary, flattened and cut to 200
characters, as ``recall`` shows it) and its source URL when that is an
http(s) URL. Anything else — unknown key, another resource's key (the
mapping is per resource, so a reused slug's new resource does not know the
old keys), a deleted memory, an unpublished or switched-off endpoint — is
``null``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.public.publication import load_serving_endpoint
from mcp_server.public.scope import public_rows
from models.memory import Memory
from models.public_mcp_usage import PublicMcpDocKey
from services.guardrail_digest import cut_summary, flatten_summary
from services.observability_scope import ScopedResource

DOCUMENT_TITLE_MAX_CHARS = 200


def public_source_url(uri: str | None) -> str | None:
    """``uri`` when it is an absolute http(s) URL, else ``None`` (the #2675 rule)."""
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return uri
    return None


@dataclass(frozen=True)
class ResolvedDocuments:
    """Per requested key, ``{"title", "source_url"}`` or ``None``."""

    documents: dict[str, dict[str, str | None] | None]
    publication_serving: bool


async def resolve_doc_keys(
    db: AsyncSession, resource: ScopedResource, doc_keys: list[str]
) -> ResolvedDocuments:
    """Resolve ``doc_keys`` of one in-scope resource through its live publication.

    Args:
        db: Session (nothing is written).
        resource: The resolved, in-scope resource.
        doc_keys: Distinct 32-hex keys (at most 100; the route checks).

    Returns:
        Every requested key, resolved or ``None``, and whether the
        publication serves right now.
    """
    documents: dict[str, dict[str, str | None] | None] = dict.fromkeys(doc_keys)
    endpoint = (
        await load_serving_endpoint(db, resource.endpoint.public_id)
        if resource.endpoint is not None
        else None
    )
    if endpoint is None or endpoint.resource_pk != resource.resource_pk:
        return ResolvedDocuments(documents=documents, publication_serving=False)

    mapping = dict(
        (
            await db.execute(
                select(PublicMcpDocKey.doc_key, PublicMcpDocKey.resource_doc_id).where(
                    PublicMcpDocKey.resource_pk == resource.resource_pk,
                    PublicMcpDocKey.doc_key.in_(doc_keys),
                )
            )
        )
        .tuples()
        .all()
    )
    if mapping:
        rows = (
            await db.execute(
                select(Memory.resource_doc_id, Memory.summary, Memory.source_uri)
                .where(public_rows(endpoint), Memory.resource_doc_id.in_(set(mapping.values())))
                .order_by(
                    Memory.resource_doc_id,
                    func.coalesce(Memory.updated_at, Memory.created_at).desc(),
                    Memory.id,
                )
            )
        ).all()
        by_doc: dict[str, dict[str, str | None]] = {}
        for doc_id, summary, source_uri in rows:
            if doc_id is None or doc_id in by_doc:
                continue
            by_doc[doc_id] = {
                "title": cut_summary(flatten_summary(summary or ""), DOCUMENT_TITLE_MAX_CHARS),
                "source_url": public_source_url(source_uri),
            }
        for key, doc_id in mapping.items():
            documents[key] = by_doc.get(doc_id)
    return ResolvedDocuments(documents=documents, publication_serving=True)
