"""MCP tool handlers for platform-managed file storage (Issue #485).

Five thin handlers that route through ``FileStorageService`` — the
exact same service the REST routes use, so the #332 lesson (REST and
MCP must funnel through one quota check) is enforced structurally.

Tool names use the verb-first convention shared with the rest of the
MCP surface (``init_file_upload`` / ``complete_file_upload`` /
``get_file_download_url`` / ``list_files`` / ``delete_file``). All
five operate on a workspace (no ``context_id``) so they live in
``_TOOLS_WITHOUT_CONTEXT_ID``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _error_response,
    _success_response,
)
from services.file_storage_service import FileStorageService
from utils.datetime import to_utc_iso
from utils.exceptions import (
    ConflictError,
    ExternalServiceError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _exc_to_error_response(exc: Exception) -> list[TextContent]:
    """Map a service-layer exception to an MCP error response.

    Mirrors the global REST exception handler so MCP clients see the
    same vocabulary as REST callers.
    """
    if isinstance(exc, ValidationError):
        return _error_response("validation_error", str(exc))
    if isinstance(exc, NotFoundException):
        return _error_response("not_found", str(exc))
    if isinstance(exc, ConflictError):
        return _error_response("conflict", str(exc))
    if isinstance(exc, QuotaExceededError):
        return _error_response("quota_exceeded", str(exc))
    if isinstance(exc, ExternalServiceError):
        # R2 5xx / AccessDenied / throttling — reachable from
        # ``confirm_upload`` and ``get_presigned_download``. Surface as a
        # named error so clients can retry with backoff rather than
        # treating it as an opaque 500.
        return _error_response("service_unavailable", str(exc))
    raise exc  # unexpected — let the dispatch layer log a 500


async def _resolve_workspace(
    workspace_id_arg: Any,
    fallback: UUID | None,
) -> tuple[UUID | None, list[TextContent] | None]:
    """Resolve the workspace_id from the tool args, falling back to the
    authenticated workspace_id if not specified.

    Returns ``(workspace_id, error)`` — exactly one is non-None.
    """
    if workspace_id_arg is None:
        if fallback is None:
            return None, _error_response("workspace_required", "No active workspace.")
        return fallback, None
    try:
        return UUID(str(workspace_id_arg)), None
    except (ValueError, TypeError):
        return None, _error_response(
            "validation_error",
            f"workspace_id must be a UUID, got {workspace_id_arg!r}",
        )


# ---------------------------------------------------------------------------
# init_file_upload
# ---------------------------------------------------------------------------


async def handle_init_file_upload(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Reserve quota and return a presigned PUT URL.

    Required args: ``filename``, ``content_type``, ``size_bytes``, ``sha256``.
    Optional: ``workspace_id`` (overrides the authenticated workspace).
    """
    required = {"filename", "content_type", "size_bytes", "sha256"}
    missing = required - args.keys()
    if missing:
        return _error_response("missing_fields", f"Missing required fields: {sorted(missing)}")

    ws, err = await _resolve_workspace(args.get("workspace_id"), workspace_id)
    if err is not None:
        return err

    from db.base import get_db

    async for db in get_db():
        viewer_err = await _check_viewer_permission(db, user_id, ws, "upload files")
        if viewer_err is not None:
            return viewer_err
        service = FileStorageService(db)
        try:
            result = await service.reserve_upload(
                workspace_id=ws,
                created_by=user_id,
                filename=str(args["filename"]),
                content_type=str(args["content_type"]),
                size_bytes=int(args["size_bytes"]),
                sha256=str(args["sha256"]).lower(),
            )
        except (ValidationError, ConflictError, QuotaExceededError, NotFoundException) as exc:
            return _exc_to_error_response(exc)
    return _success_response(
        file_id=str(result.file_id),
        upload_url=result.upload_url,
        expires_at=to_utc_iso(result.expires_at),
    )


# ---------------------------------------------------------------------------
# complete_file_upload
# ---------------------------------------------------------------------------


async def handle_complete_file_upload(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Verify the upload landed and finalize the row.

    Required args: ``file_id``, ``sha256``.
    Optional: ``workspace_id``.
    """
    required = {"file_id", "sha256"}
    missing = required - args.keys()
    if missing:
        return _error_response("missing_fields", f"Missing required fields: {sorted(missing)}")

    ws, err = await _resolve_workspace(args.get("workspace_id"), workspace_id)
    if err is not None:
        return err

    try:
        file_id = UUID(str(args["file_id"]))
    except (ValueError, TypeError):
        return _error_response("validation_error", "file_id must be a UUID")

    from db.base import get_db

    async for db in get_db():
        viewer_err = await _check_viewer_permission(db, user_id, ws, "confirm file uploads")
        if viewer_err is not None:
            return viewer_err
        service = FileStorageService(db)
        try:
            file = await service.confirm_upload(
                workspace_id=ws,
                file_id=file_id,
                sha256=str(args["sha256"]).lower(),
            )
        except (
            ValidationError,
            ConflictError,
            NotFoundException,
            ExternalServiceError,
        ) as exc:
            return _exc_to_error_response(exc)
    return _success_response(
        file_id=str(file.id),
        status=file.status,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
    )


# ---------------------------------------------------------------------------
# get_file_download_url
# ---------------------------------------------------------------------------


async def handle_get_file_download_url(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Return a short-lived presigned GET URL.

    Required args: ``file_id``.
    Optional: ``workspace_id``.
    """
    if "file_id" not in args:
        return _error_response("missing_fields", "Missing required field: file_id")

    ws, err = await _resolve_workspace(args.get("workspace_id"), workspace_id)
    if err is not None:
        return err

    try:
        file_id = UUID(str(args["file_id"]))
    except (ValueError, TypeError):
        return _error_response("validation_error", "file_id must be a UUID")

    from db.base import get_db

    async for db in get_db():
        # Workspace membership MUST be enforced before reading. The
        # auth layer only proves identity, not membership of the
        # ``workspace_id`` the caller supplied. Without this, an
        # authenticated MCP caller could pass another workspace's UUID
        # via the ``workspace_id`` arg and obtain download URLs for
        # files they shouldn't be able to read.
        viewer_err = await _check_viewer_permission(db, user_id, ws, "download files")
        if viewer_err is not None:
            return viewer_err
        service = FileStorageService(db)
        try:
            url = await service.get_presigned_download(
                workspace_id=ws,
                file_id=file_id,
            )
        except (ValidationError, NotFoundException, ExternalServiceError) as exc:
            return _exc_to_error_response(exc)
    return _success_response(download_url=url)


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


async def handle_delete_file(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Soft-delete a file and immediately release the quota.

    Required args: ``file_id``.
    Optional: ``workspace_id``.
    """
    if "file_id" not in args:
        return _error_response("missing_fields", "Missing required field: file_id")

    ws, err = await _resolve_workspace(args.get("workspace_id"), workspace_id)
    if err is not None:
        return err

    try:
        file_id = UUID(str(args["file_id"]))
    except (ValueError, TypeError):
        return _error_response("validation_error", "file_id must be a UUID")

    from db.base import get_db

    async for db in get_db():
        viewer_err = await _check_viewer_permission(db, user_id, ws, "delete files")
        if viewer_err is not None:
            return viewer_err
        service = FileStorageService(db)
        try:
            await service.delete_file(workspace_id=ws, file_id=file_id)
        except NotFoundException as exc:
            return _exc_to_error_response(exc)
    return _success_response(file_id=str(file_id), deleted=True)


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


async def handle_list_files(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List uploaded, non-deleted files in the workspace, newest first.

    Optional args: ``workspace_id``, ``limit`` (default 50).
    """
    ws, err = await _resolve_workspace(args.get("workspace_id"), workspace_id)
    if err is not None:
        return err

    try:
        limit = int(args.get("limit", 50))
    except (ValueError, TypeError):
        return _error_response("validation_error", "limit must be an integer")

    from db.base import get_db

    async for db in get_db():
        # Membership gate (same reasoning as handle_get_file_download_url):
        # without this an authenticated caller could enumerate another
        # workspace's file metadata by supplying its UUID via the
        # ``workspace_id`` arg.
        viewer_err = await _check_viewer_permission(db, user_id, ws, "list files")
        if viewer_err is not None:
            return viewer_err
        service = FileStorageService(db)
        try:
            files = await service.list_files(workspace_id=ws, limit=limit)
        except ValidationError as exc:
            return _exc_to_error_response(exc)
    return _success_response(
        files=[
            {
                "id": str(f.id),
                "filename": f.filename,
                "content_type": f.content_type,
                "size_bytes": f.size_bytes,
                "sha256": f.sha256,
                "status": f.status,
                "created_at": to_utc_iso(f.created_at) if f.created_at else None,
                "uploaded_at": to_utc_iso(f.uploaded_at) if f.uploaded_at else None,
            }
            for f in files
        ],
        count=len(files),
    )
