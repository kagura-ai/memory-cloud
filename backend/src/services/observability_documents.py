"""Resolve opaque document keys to what the public endpoint already shows (#2679).

A ``doc_key`` (#2678) is an HMAC of ``(resource_pk, resource doc id)``; it
cannot be inverted. ``public_mcp_doc_keys`` (written with the usage events)
maps it back to the document id of that one resource. A key resolves only
when the resource's publication is **currently serving**
(``load_serving_endpoint``) and a memory of that document passes
``scope.public_rows`` — the same rows anonymous ``recall`` returns. Then the
answer is the document's title (what ``recall`` shows for it — the summary,
or for an unedited feed record its title or question — flattened and cut to
200 characters) and its source URL (the record's URL, or the row's source
URL when that is an http(s) URL). Anything else — unknown key, another
resource's key (the mapping is per resource, so a reused slug's new resource
does not know the old keys), a deleted memory, an unpublished or switched-off
endpoint — is ``null``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.public.feed_record import (
    feed_content_column,
    parse_feed_record,
    public_source_url,
)
from mcp_server.public.publication import ServingEndpoint, load_serving_endpoint
from mcp_server.public.scope import public_rows
from models.memory import Memory
from models.public_mcp_usage import PublicMcpDocKey
from services.guardrail_digest import cut_summary, flatten_summary
from services.observability_scope import ScopedResource

DOCUMENT_TITLE_MAX_CHARS = 200


@dataclass(frozen=True)
class ResolvedDocuments:
    """Per requested key, ``{"title", "source_url"}`` or ``None``."""

    documents: dict[str, dict[str, str | None] | None]
    publication_serving: bool


async def resolve_public_documents(
    db: AsyncSession, endpoint: ServingEndpoint, doc_keys: list[str]
) -> dict[str, dict[str, str | None] | None]:
    """Resolve ``doc_keys`` through ``endpoint``, which the caller found serving.

    One row per document is read: its newest row that passes ``public_rows``.
    The title is what anonymous ``recall`` shows for that row (for an unedited
    feed record its title or question, #3032), flattened and cut to
    ``DOCUMENT_TITLE_MAX_CHARS``; the URL is the record's URL or the row's
    http(s) source URL.

    Args:
        db: Session (nothing is written).
        endpoint: The serving publication of the keys' resource.
        doc_keys: Distinct 32-hex keys.

    Returns:
        Every requested key, resolved or ``None``.
    """
    documents: dict[str, dict[str, str | None] | None] = dict.fromkeys(doc_keys)
    mapping = dict(
        (
            await db.execute(
                select(PublicMcpDocKey.doc_key, PublicMcpDocKey.resource_doc_id).where(
                    PublicMcpDocKey.resource_pk == endpoint.resource_pk,
                    PublicMcpDocKey.doc_key.in_(doc_keys),
                )
            )
        )
        .tuples()
        .all()
    )
    if not mapping:
        return documents
    rows = (
        await db.execute(
            select(
                Memory.resource_doc_id,
                Memory.summary,
                Memory.source_uri,
                feed_content_column(),
            )
            .distinct(Memory.resource_doc_id)
            .where(public_rows(endpoint), Memory.resource_doc_id.in_(set(mapping.values())))
            .order_by(
                Memory.resource_doc_id,
                func.coalesce(Memory.updated_at, Memory.created_at).desc(),
                Memory.id,
            )
        )
    ).all()
    by_doc: dict[str, dict[str, str | None]] = {}
    for doc_id, summary, source_uri, feed_content in rows:
        if doc_id is None or doc_id in by_doc:
            continue
        record = parse_feed_record(feed_content)
        title = record.label if record is not None else summary or ""
        by_doc[doc_id] = {
            "title": cut_summary(flatten_summary(title), DOCUMENT_TITLE_MAX_CHARS),
            "source_url": (record.url if record is not None else None)
            or public_source_url(source_uri),
        }
    for key, doc_id in mapping.items():
        documents[key] = by_doc.get(doc_id)
    return documents


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
    endpoint = (
        await load_serving_endpoint(db, resource.endpoint.public_id)
        if resource.endpoint is not None
        else None
    )
    if endpoint is None or endpoint.resource_pk != resource.resource_pk:
        return ResolvedDocuments(documents=dict.fromkeys(doc_keys), publication_serving=False)
    return ResolvedDocuments(
        documents=await resolve_public_documents(db, endpoint, doc_keys),
        publication_serving=True,
    )
