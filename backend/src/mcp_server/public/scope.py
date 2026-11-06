"""The one predicate every public data read is built from (#2675).

A memory is public on an endpoint when it lives in the bound workspace and
context, was ingested for the bound resource (the computed
``memories.resource_id`` column equals the slug — a note a member wrote by
hand into the same context has no ``resource_id``), and is not deleted. A row
that fails it does not exist for the public surface: not in results, not as a
graph neighbour, not in tags, not in an error. ``recall``, ``reference``,
``explore`` and ``list_tags`` all filter through here (list_tags passes the
same three columns to the shared tag query).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_

from mcp_server.public.publication import ServingEndpoint
from models.memory import Memory


def public_rows(endpoint: ServingEndpoint) -> ColumnElement[bool]:
    """The SQL condition a ``memories`` row must meet to be served by ``endpoint``."""
    return and_(
        Memory.workspace_id == endpoint.workspace_id,
        Memory.context_id == endpoint.context_id,
        Memory.resource_id == endpoint.resource_id,
        Memory.deleted_at.is_(None),
    )
