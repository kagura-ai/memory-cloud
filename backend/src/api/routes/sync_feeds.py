"""Installation-only service feed protocol; no general resource or credential APIs."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.sync_worker import SyncWorkerError, SyncWorkerRoute
from auth.sync_bearer import sync_service_token
from db.base import get_db
from models.sync_feed_schemas import (
    DEFINITIONS,
    MAX_CHUNK_BYTES,
    MAX_RECORDS,
    MAX_RUN_BYTES,
    SCHEMA_HASH,
    ChunkBody,
    ConfirmDiff,
    RunManifest,
)
from services import sync_feed_runs as runs
from services.sync_credentials import CredentialAuthority, sync_token_boundary
from services.sync_feed_authority import FeedFence, feed_fence, runs_enabled, worker_target
from services.sync_feed_commit import commit_run
from services.sync_feed_index import index_status
from utils.exceptions import SyncProtocolError

router = APIRouter(
    prefix="/sync/installations/{installation_id}",
    tags=["workspace-sync"],
    route_class=SyncWorkerRoute,
    dependencies=[Depends(runs_enabled)],
    responses={
        status: {"model": SyncWorkerError}
        for status in (400, 401, 403, 404, 409, 429, 503, "default")
    },
)
Token = Annotated[str, Depends(sync_service_token)]
Database = Annotated[AsyncSession, Depends(get_db)]
Body = TypeVar("Body", bound=BaseModel)


def json_body(model: type[BaseModel]) -> dict:
    """Document the same strict contract used by the bounded stream decoder."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items()}
        return [inline(item) for item in value] if isinstance(value, list) else value

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": inline(schema)}},
        }
    }


def exact_query(request: Request, allowed: set[str] | None = None) -> None:
    """Reject duplicate or unknown selectors instead of silently dropping them."""
    keys = list(request.query_params.keys())
    if not set(keys).issubset(allowed or set()) or any(
        len(request.query_params.getlist(key)) != 1 for key in keys
    ):
        raise SyncProtocolError("invalid_request")


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object must not contain ambiguous repeated keys."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


async def bounded_body(
    request: Request, model: type[Body], maximum: int = 8192
) -> tuple[Body, int]:
    """Bound allocation before entering any paid transaction or row locks."""
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise SyncProtocolError("invalid_request")
    payload = bytearray()
    async for part in request.stream():
        if len(payload) + len(part) > maximum:
            raise SyncProtocolError("request_too_large", 413)
        payload.extend(part)
    try:
        json.loads(payload, object_pairs_hook=unique_object)
        return model.model_validate_json(bytes(payload)), len(payload)
    except (ValueError, ValidationError, UnicodeError, RecursionError):
        raise SyncProtocolError("invalid_request") from None


@asynccontextmanager
async def worker(
    db: AsyncSession, token: str, iid: UUID, operation: str, *, write: bool = False
) -> AsyncIterator[tuple[CredentialAuthority, FeedFence]]:
    """The new finite run fence outlives the existing guard's actual commit."""
    async with feed_fence(db) as fence:
        async with sync_token_boundary(db, token, iid, operation=operation) as authority:
            await worker_target(db, authority.installation, content_write=write)
            yield authority, fence


@router.get("/schema")
async def feed_schema(installation_id: UUID, request: Request, token: Token, db: Database) -> dict:
    """Read the fixed public wire contract for this exact installation."""
    exact_query(request)
    async with worker(db, token, installation_id, "schema:read") as (authority, _fence):
        await runs.schema_matches(db, authority)
        from services.sync_feed_authority import current_schema

        schema = await current_schema(db, authority.installation)
        result = {
            "schema_hash": SCHEMA_HASH,
            "schema_version": schema.schema_version if schema else 1,
            "fields": DEFINITIONS,
            "kinds": ["product", "faq"],
            "limits": {
                "feeds": 20,
                "records": 10000,
                "chunks": 1000,
                "chunk_records": 100,
                "chunk_bytes": MAX_CHUNK_BYTES,
                "run_bytes": MAX_RUN_BYTES,
                "run_ttl_seconds": 86400,
                "approval_ttl_seconds": 900,
            },
        }
    return result


@router.post("/runs", status_code=201, openapi_extra=json_body(RunManifest))
async def new_feed_run(installation_id: UUID, request: Request, token: Token, db: Database) -> dict:
    """Create or recover a durable operation receipt without publishing content."""
    exact_query(request)
    body, _size = await bounded_body(request, RunManifest)
    async with worker(db, token, installation_id, "records:write", write=True) as (
        authority,
        _fence,
    ):
        result = runs.run_view(await runs.create_run(db, authority, body))
    return result


@router.put("/runs/{run_id}/chunks/{chunk_index}", openapi_extra=json_body(ChunkBody))
async def upload_feed_chunk(
    installation_id: UUID,
    run_id: UUID,
    chunk_index: Annotated[int, Path(ge=0, le=999)],
    request: Request,
    token: Token,
    db: Database,
) -> dict:
    """Stage one bounded chunk; retries retain the same immutable digest receipt."""
    exact_query(request)
    body, size = await bounded_body(request, ChunkBody, MAX_CHUNK_BYTES)
    async with worker(db, token, installation_id, "records:write", write=True) as (
        authority,
        fence,
    ):
        result = runs.run_view(
            await runs.stage_chunk(db, authority, run_id, chunk_index, body, size, fence)
        )
    return result


@router.post("/runs/{run_id}/prepare")
async def prepare_feed_run(
    installation_id: UUID, run_id: UUID, request: Request, token: Token, db: Database
) -> dict:
    """Validate completeness and bind a finite exact diff before publication."""
    exact_query(request)
    async with worker(db, token, installation_id, "records:write", write=True) as (
        authority,
        fence,
    ):
        result = runs.run_view(await runs.prepare_run(db, authority, run_id, fence))
    return result


@router.get("/runs/{run_id}")
async def inspect_feed_run(
    installation_id: UUID, run_id: UUID, request: Request, token: Token, db: Database
) -> dict:
    """Recover a lost response and distinguish canonical commit from search progress."""
    exact_query(request)
    async with worker(db, token, installation_id, "results:read") as (authority, _fence):
        row, feed = await runs.run_for(db, authority, run_id)
        result = {**runs.run_view(row), "index_status": await index_status(db, row, feed)}
    return result


@router.get("/runs/{run_id}/diff")
async def inspect_feed_diff(
    installation_id: UUID,
    run_id: UUID,
    request: Request,
    token: Token,
    db: Database,
    offset: Annotated[int, Query(ge=0, le=2 * MAX_RECORDS)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    """Read a bounded page of prepared change identities and hashes."""
    exact_query(request, {"offset", "limit"})
    async with worker(db, token, installation_id, "results:read") as (authority, _fence):
        row, _feed = await runs.run_for(db, authority, run_id)
        result = {
            **runs.run_view(row),
            "diff": (row.diff or [])[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(row.diff or []) else None,
        }
    return result


@router.post("/runs/{run_id}/commit", openapi_extra=json_body(ConfirmDiff))
async def commit_feed_run(
    installation_id: UUID, run_id: UUID, request: Request, token: Token, db: Database
) -> dict:
    """Atomically commit this feed and its durable resource-indexing handoff."""
    exact_query(request)
    body, _size = await bounded_body(request, ConfirmDiff)
    async with worker(db, token, installation_id, "records:write", write=True) as (
        authority,
        fence,
    ):
        result = runs.run_view(await commit_run(db, authority, run_id, body, fence))
    return result


@router.post("/runs/{run_id}/abort")
async def abort_feed_run(
    installation_id: UUID, run_id: UUID, request: Request, token: Token, db: Database
) -> dict:
    """Clear uncommitted payloads, retaining retry receipts and committed data."""
    exact_query(request)
    async with worker(db, token, installation_id, "records:write") as (authority, _fence):
        result = runs.run_view(await runs.abort_run(db, authority, run_id))
    return result
