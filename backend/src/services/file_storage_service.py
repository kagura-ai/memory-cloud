"""``FileStorageService`` — Phase 1 platform-managed file storage (Issue #485).

Single shared service for both the REST router (``/api/v1/files/*``) and
the MCP tool layer (``init_file_upload`` / ``complete_file_upload`` /
``get_file_download_url`` / ``delete_file``). The #332 lesson is enforced
structurally: there is exactly one place that calls
``storage_quota_service.reserve_storage_bytes`` and one place that
generates presigned R2 URLs — this class.

Upload state machine (R3):

    upload-init  ──INSERT(reserved)──▶  client PUTs to presigned URL
                                              │
                                              ▼
                                   upload-complete (head_object OK)
                                              │
                                              ▼
                                          uploaded
                                              │
                                              ▼
                              soft-delete (R5: quota released here)
                                              │
                                              ▼
                                   nightly GC (R2 binary deleted)

Orphan path: ``status='reserved'`` rows past ``expires_at + 1h`` are
swept by the orphan task (Commit 8) — the sweeper calls
``release_storage_bytes`` and deletes any orphan R2 object.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import Workspace
from models.file_objects import FileObject, WorkspaceStorageUsage
from services import storage_quota_service
from services.permission_service import PermissionService
from storage.factory import get_blob_storage
from storage.protocol import BlobStorageProtocol
from utils.datetime import utcnow
from utils.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundException,
    UnsupportedMediaTypeError,
    ValidationError,
)
from utils.hashing import SHA256_HEX_PATTERN
from utils.logger import get_logger
from utils.media_types import MEDIA_TYPE_RE, normalize_media_type

# Strict char-by-char match for the on-wire sha256 shape — distinct from
# ``bytes.fromhex`` which silently skips internal whitespace and would
# accept a 64-char input with embedded whitespace, decoding to fewer
# than 32 bytes (exact length depends on how many non-hex chars are present).
_SHA256_HEX_RE = re.compile(SHA256_HEX_PATTERN)

# Issue #961: extension→MIME resolver for the content_type consistency check.
# A private ``MimeTypes`` instance is seeded ONLY from Python's compiled-in
# table — it does not read the host's ``/etc/mime.types`` — so the
# security-relevant mapping (e.g. ``.svg`` → ``image/svg+xml``) is identical
# across every deploy environment and does not probe the filesystem on the
# request path. Built once at import; ``guess_type`` is read-only thereafter.
_MIME = mimetypes.MimeTypes()

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReserveResult:
    """Returned by ``reserve_upload`` — what the REST/MCP handler echoes
    back to the client."""

    file_id: UUID
    upload_url: str
    expires_at: datetime


class FileStorageService:
    """One row, one service. Both REST and MCP funnel through here.

    The class is intentionally light: complex behaviour (Redis
    reservation, presigned URL generation, blob backend selection)
    lives in the dedicated modules
    (``storage_quota_service``, ``storage.factory``, ``storage.r2``).
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        storage: BlobStorageProtocol | None = None,
    ) -> None:
        self.db = db
        self._storage_override = storage

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _storage(self) -> BlobStorageProtocol:
        """Resolve the blob backend lazily so unit tests can inject a fake
        without paying the ``get_blob_storage`` cost."""
        if self._storage_override is not None:
            return self._storage_override
        return get_blob_storage()

    @staticmethod
    def _build_storage_key(workspace_id: UUID, sha256: str) -> str:
        """Bucket key per issue spec: ``{workspace_id}/{sha256[:2]}/{sha256}``.

        Per-workspace prefix supports per-prefix lifecycle/ACL policies;
        sha256[:2] sub-prefix avoids R2 hot-partition on workloads that
        upload many files in quick succession.

        Caller is the only public path (``reserve_upload``) and that
        path validates ``len(sha256) == 64`` upstream — no defensive
        re-check here.
        """
        return f"{workspace_id}/{sha256[:2]}/{sha256}"

    async def _load_workspace(self, workspace_id: UUID) -> Workspace:
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()
        if workspace is None:
            msg = f"workspace {workspace_id} not found"
            raise NotFoundException(msg)
        return workspace

    async def _load_file(
        self,
        workspace_id: UUID,
        file_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> FileObject:
        """Load a file_objects row, enforcing workspace boundary.

        Cross-workspace access raises ``NotFoundException`` (not 403) so
        the existence of a file in another workspace is not leaked.
        """
        result = await self.db.execute(
            select(FileObject).where(
                FileObject.id == file_id,
                FileObject.workspace_id == workspace_id,
            )
        )
        file = result.scalar_one_or_none()
        if file is None:
            msg = f"file {file_id} not found in workspace {workspace_id}"
            raise NotFoundException(msg)
        if not include_deleted and file.deleted_at is not None:
            msg = f"file {file_id} not found in workspace {workspace_id}"
            raise NotFoundException(msg)
        return file

    # ------------------------------------------------------------------
    # Upload flow (R3)
    # ------------------------------------------------------------------

    async def reserve_upload(
        self,
        *,
        workspace_id: UUID,
        created_by: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        context_id: UUID | None = None,
    ) -> ReserveResult:
        """Reserve quota + insert reserved row + return presigned PUT URL.

        When ``context_id`` is given (Issue #1136), the file is bound to that
        context and the caller must have **write** access to it (EDITOR+ via the
        context ACL), and the context must live in ``workspace_id``. ``None``
        keeps the legacy workspace-scoped behaviour (the caller's workspace
        membership gate is the only access control). The binding routes all later
        read/write/list access through the context's ACL.

        Raises:
            ValidationError: invalid size, sha256, or content_type — covers
                size/sha256 length checks, oversized filename/content_type,
                control characters in content_type, and malformed
                type/subtype shape (no slash, garbage chars); or a ``context_id``
                that is not in ``workspace_id``. 422 at REST.
            AuthorizationError: ``context_id`` given but the caller lacks write
                access to that context. 403 at REST.
            UnsupportedMediaTypeError: content_type passes shape validation
                but is not in ``settings.allowed_file_content_types_set``.
                415 at REST; ``unsupported_media_type`` vocab at MCP.
            QuotaExceededError: workspace storage cap would be exceeded.
            ConflictError: same ``(workspace_id, sha256)`` already has an
                active or in-flight row (partial unique violation).
            NotFoundException: workspace (or a given ``context_id``) missing.
        """
        # Issue #1136 (authz-first): a context-bound upload requires WRITE access
        # to that context (EDITOR+), and the context must belong to THIS
        # workspace — so a caller cannot bind a file created in workspace A to a
        # context in workspace B. Checked before input validation / quota so an
        # unauthorized caller learns nothing about (and spends nothing on) the
        # upload. check_context_write raises AuthorizationError (403) /
        # NotFoundException (404, incl. a soft-deleted context) on denial.
        if context_id is not None:
            ctx = await PermissionService(self.db).check_context_write(created_by, context_id)
            if ctx.workspace_id != workspace_id:
                msg = "context_id does not belong to this workspace"
                raise ValidationError(msg)

        # Enforce DB column limits at the service boundary so REST and MCP
        # share the same hard caps. Pydantic enforces these at REST via
        # ``FileReserveRequest``, but MCP coerces ``args["filename"]`` /
        # ``args["content_type"]`` with bare ``str(...)`` and would otherwise
        # let an oversized value reach ``flush()`` / ``commit()`` and surface
        # as an opaque 500 from a column-length DB error.
        if len(filename) > 512:
            msg = f"filename must be at most 512 chars, got {len(filename)}"
            raise ValidationError(msg)
        if len(content_type) > 255:
            msg = f"content_type must be at most 255 chars, got {len(content_type)}"
            raise ValidationError(msg)
        settings = get_settings()
        max_bytes = settings.file_object_max_size_mb * 1024 * 1024
        if size_bytes <= 0 or size_bytes > max_bytes:
            msg = f"size_bytes must be in (0, {max_bytes}], got {size_bytes}"
            raise ValidationError(msg)
        if len(sha256) != 64:
            msg = f"sha256 must be 64 hex chars, got {len(sha256)}"
            raise ValidationError(msg)
        if not _SHA256_HEX_RE.fullmatch(sha256):
            msg = f"sha256 must be 64 hex chars, got non-hex characters in {sha256!r}"
            raise ValidationError(msg)
        # Normalize to lowercase before any downstream use. Postgres TEXT
        # comparison is case-sensitive, so without normalization an upper-
        # case "AB…" and lower-case "ab…" of the same digest would land in
        # separate rows under the partial unique index — defeating dedup
        # and breaking ``confirm_upload``'s sha-equality check on retry.
        # MCP already lowercases at the boundary (mcp_server/tools/files.py);
        # this is the canonical normalization point for the REST path too.
        sha256 = sha256.lower()
        # Reject control characters in the raw content_type before any further
        # work — the value is later signed by R2 SigV4 as the ContentType
        # header, so an embedded CR/LF/NUL is a header-injection primitive
        # against R2 (defense-in-depth — R2 itself also validates).
        if any(c in content_type for c in "\r\n\x00"):
            msg = "content_type contains control characters"
            raise ValidationError(msg)
        # Strip RFC 7231 media-type parameters ("text/plain; charset=utf-8")
        # before compare; browsers and multipart uploads routinely attach
        # them but the allow-list is keyed on bare type/subtype. Malformed
        # shape (no slash, empty type/subtype, garbage chars) is a 422
        # validation error — distinct from the 415 policy rejection below.
        base_content_type = normalize_media_type(content_type)
        if not MEDIA_TYPE_RE.match(base_content_type):
            msg = f"content_type must be 'type/subtype' (got {content_type!r})"
            raise ValidationError(msg)
        # Empty allow-list rejects everything (fail-closed).
        allowed = settings.allowed_file_content_types_set
        if base_content_type not in allowed:
            raise UnsupportedMediaTypeError(
                content_type=content_type,
                allowed=allowed,
            )

        # Issue #961: the allow-list above only validates the *declared*
        # content_type. A disallowed payload can slip in mislabeled — an
        # ``.svg`` declared ``text/plain`` lands as text/plain (allowed) yet
        # carries inline-script SVG (stored-XSS once served). Derive the MIME
        # the filename *extension* implies and reject any inconsistency, so a
        # declared value can no longer launder a disallowed (or simply
        # mismatched) type past the allow-list. ``guess_type`` lower-cases the
        # extension, so ``.SVG`` resolves like ``.svg``. When the extension is
        # unknown to Python's mimetypes registry (``.md``, ``.webp`` → None) or
        # absent, the inferred value is None and this layer is skipped — the
        # declared allow-list remains the governing check, avoiding
        # false-positives on extensions mimetypes does not know. NOTE: as the
        # operator widens ALLOWED_FILE_CONTENT_TYPES, a declared value that is
        # allowed but differs from the extension's canonical MIME (e.g. an
        # ``.xml`` declared ``application/xml`` when mimetypes infers
        # ``text/xml``) will also be rejected here — intended strictness, but
        # worth knowing when curating the allow-list. This runs at the shared
        # service chokepoint, so REST (POST /files/reserve) and MCP
        # (handle_init_file_upload) inherit it identically (#553 parity).
        guessed_raw, _ = _MIME.guess_type(filename)
        inferred = normalize_media_type(guessed_raw) if guessed_raw else None
        if inferred is not None and inferred != base_content_type:
            raise UnsupportedMediaTypeError(
                content_type=content_type,
                allowed=allowed,
                inferred_content_type=inferred,
            )

        workspace = await self._load_workspace(workspace_id)

        # R5/R3: reserve in Redis BEFORE inserting the row so a quota
        # rejection short-circuits without touching the DB.
        await storage_quota_service.reserve_storage_bytes(
            workspace_id=workspace_id,
            size_bytes=size_bytes,
            quota_bytes=workspace.effective_storage_limit_bytes,
            db=self.db,
        )

        try:
            file_id = uuid4()
            storage_key = self._build_storage_key(workspace_id, sha256)
            now = utcnow()
            expires_at = now + timedelta(seconds=settings.presign_put_ttl_seconds)

            file = FileObject(
                id=file_id,
                workspace_id=workspace_id,
                context_id=context_id,
                sha256=sha256,
                size_bytes=size_bytes,
                content_type=content_type,
                filename=filename,
                storage_backend="r2",
                storage_key=storage_key,
                status="reserved",
                expires_at=expires_at,
                created_by=created_by,
                created_at=now,
            )
            self.db.add(file)
            await self.db.flush()
            upload_url = await self._storage.generate_presigned_put(
                key=storage_key,
                content_type=content_type,
                size_bytes=size_bytes,
                ttl_seconds=settings.presign_put_ttl_seconds,
                sha256=sha256,
            )
            await self.db.commit()
        except Exception as exc:
            # Roll the Redis reservation back — neither the DB row nor the
            # presigned URL are durable. Covers all three failure modes:
            # flush() (partial-unique conflict), presign (R2 transient
            # error), and commit() (DB consistency). Without this, the
            # Redis counter would stay incremented until the 24h reseed
            # — over-counting the workspace by ``size_bytes`` and
            # potentially blocking later legitimate uploads on small
            # plans.
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=size_bytes,
            )
            # Detect the partial unique violation without depending on a
            # specific dialect class — the index name surfaces in the
            # message. Look up the existing row and include its file_id
            # in the error so SDK callers can switch to it directly
            # instead of running a separate dedup query (the docstring
            # of this method advertises this idempotent-retry path).
            if "uq_file_objects_workspace_sha256_active" in str(exc):
                existing_id = None
                try:
                    existing = await self.db.execute(
                        select(FileObject.id, FileObject.context_id).where(
                            FileObject.workspace_id == workspace_id,
                            FileObject.sha256 == sha256,
                            FileObject.deleted_at.is_(None),
                            FileObject.status != "failed",
                        )
                    )
                    row = existing.first()
                    # #1136: dedup is workspace-scoped but access is context-
                    # scoped. Only surface the existing file_id when the prior
                    # row is in the SAME context the caller is uploading to
                    # (including both NULL) — they already passed the write check
                    # for that context. A cross-context conflict omits the id so
                    # the caller cannot learn the file_id of a file bound to a
                    # context they cannot access.
                    if row is not None and row.context_id == context_id:
                        existing_id = row.id
                except Exception:  # noqa: BLE001 — best-effort enrichment
                    pass
                msg = f"file with sha256={sha256} already exists in workspace"
                if existing_id is not None:
                    msg += f"; reuse file_id={existing_id}"
                conflict = ConflictError(msg)
                raise conflict from exc
            raise

        logger.info(
            "file_upload_reserved",
            workspace_id=str(workspace_id),
            file_id=str(file_id),
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return ReserveResult(
            file_id=file_id,
            upload_url=upload_url,
            expires_at=expires_at,
        )

    async def confirm_upload(
        self,
        *,
        workspace_id: UUID,
        file_id: UUID,
        sha256: str,
        actor_user_id: str | None = None,
    ) -> FileObject:
        """Verify the upload landed in R2 and finalize the row.

        Idempotent on retry: if the row is already ``status='uploaded'``
        and the sha256 matches, return the existing row unchanged.

        Issue #1136: if the file is context-bound, the caller must have write
        access to that context (the reserve gated it, but confirm may be a later
        or different caller) — checked after the workspace boundary load.

        The caller's claimed-sha256 check below is a defense-in-depth
        guard for caller-side drift between reservation and confirm —
        it runs regardless of whether ``r2_checksum_binding_enabled``
        is True (#556). When the flag is on, the storage-side sha256
        binding in ``generate_presigned_put`` is the primary integrity
        gate (R2 rejects mismatched bytes at PUT time).

        Raises:
            NotFoundException: file or its workspace not found.
            ValidationError: sha256 mismatch (caller / row-state drift).
            ConflictError: ``head_object`` says the binary is missing.
        """
        file = await self._load_file(workspace_id, file_id)

        # Issue #1136: context-bound files require write access to their context.
        # actor_user_id is optional in the signature (internal/legacy callers of
        # workspace-scoped files may omit it), but a context-bound file with no
        # actor is denied — fail-closed.
        if file.context_id is not None:
            if not actor_user_id:
                raise AuthorizationError("Insufficient permissions")
            await PermissionService(self.db).check_context_write(actor_user_id, file.context_id)

        # Normalize caller sha256 to lowercase for symmetry with reserve_upload's
        # canonicalization (Copilot review #574 finding). Without this, an SDK
        # that uppercases its claim would falsely trip the sha-mismatch guard
        # against the lowercase-stored reservation.
        sha256 = sha256.lower()

        # Idempotent retry path
        if file.status == "uploaded":
            if file.sha256 != sha256:
                msg = f"sha256 mismatch on retry: stored={file.sha256[:8]}…, caller={sha256[:8]}…"
                raise ValidationError(msg)
            return file

        if file.status != "reserved":
            msg = f"file {file_id} is in status={file.status!r}; cannot confirm"
            raise ValidationError(msg)

        if file.sha256 != sha256:
            msg = (
                f"sha256 mismatch: reservation declared {file.sha256[:8]}…, "
                f"upload reports {sha256[:8]}…"
            )
            raise ValidationError(msg)

        # R2 head_object verifies the client actually wrote bytes
        meta = await self._storage.head_object(file.storage_key or "")
        if meta is None:
            msg = (
                f"R2 object missing for file {file_id} (key={file.storage_key}); "
                "presigned PUT may have expired or never happened"
            )
            raise ConflictError(msg)
        if meta["size_bytes"] > file.size_bytes:
            # R2 reports MORE bytes than the client declared at reservation
            # time. This bypasses the per-file 100MB cap and over-charges
            # quota — reject hard. Operator may need to delete the orphan
            # R2 object manually.
            msg = (
                f"R2 object for file {file_id} is {meta['size_bytes']} bytes "
                f"but reservation declared only {file.size_bytes}; rejecting "
                "as malformed upload"
            )
            raise ConflictError(msg)

        # Truncation refund — only the underflow case (R2 < reserved)
        # gives bytes back to the workspace quota. ``release`` is
        # deferred until AFTER ``db.commit`` succeeds (R5: Redis
        # follows the durable-DB-state truth, not the other way around).
        truncation_refund = 0
        if meta["size_bytes"] < file.size_bytes:
            truncation_refund = file.size_bytes - meta["size_bytes"]
            file.size_bytes = meta["size_bytes"]

        file.status = "uploaded"
        file.uploaded_at = utcnow()
        file.expires_at = None  # uploaded rows are durable; expires_at is sweeper-only

        await self._upsert_workspace_usage(workspace_id, delta_bytes=file.size_bytes, delta_files=1)

        await self.db.commit()
        await self.db.refresh(file)

        if truncation_refund > 0:
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=truncation_refund,
            )

        logger.info(
            "file_upload_confirmed",
            workspace_id=str(workspace_id),
            file_id=str(file_id),
            size_bytes=file.size_bytes,
        )
        return file

    # ------------------------------------------------------------------
    # Read / download
    # ------------------------------------------------------------------

    async def list_files(
        self,
        *,
        workspace_id: UUID,
        accessible_context_ids: list[UUID] | None = None,
        limit: int = 50,
    ) -> list[FileObject]:
        """Return uploaded, non-deleted files the caller can access, newest first.

        Issue #1136: a row is visible when it is workspace-scoped (``context_id``
        IS NULL — legacy behaviour) OR bound to a context in
        ``accessible_context_ids``. The caller (REST / MCP) computes that set via
        ``PermissionService.get_accessible_contexts`` — which already honours
        private/shared and ``allowed_context_ids`` — and passes it here so the
        filter is applied IN the query, BEFORE the ``LIMIT``: a viewer never sees
        the existence of files scoped away from them, and the limit isn't
        silently consumed by rows that would be filtered out (the #317
        pre-filter-before-aggregate lesson).

        The filter is ALWAYS applied (fail-closed): ``None`` or an empty list is
        treated as an empty accessible set, so only workspace-scoped (NULL)
        files are returned — an internal caller that forgets the argument can
        never leak context-bound files. The REST and MCP list handlers always
        pass the real computed set.
        """
        if limit <= 0 or limit > 500:
            msg = f"limit must be in (0, 500], got {limit}"
            raise ValidationError(msg)

        # Workspace-scoped (NULL) files are always visible; context-bound files
        # only when their context is in the caller's accessible set. Applied
        # unconditionally — None/[] ⇒ NULL-context files only (fail-closed).
        visibility = FileObject.context_id.is_(None)
        if accessible_context_ids:
            visibility = visibility | FileObject.context_id.in_(accessible_context_ids)

        result = await self.db.execute(
            select(FileObject)
            .where(
                FileObject.workspace_id == workspace_id,
                FileObject.deleted_at.is_(None),
                FileObject.status == "uploaded",
                visibility,
            )
            .order_by(FileObject.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_presigned_download(
        self,
        *,
        workspace_id: UUID,
        file_id: UUID,
        actor_user_id: str | None = None,
    ) -> str:
        """Return a short-lived presigned GET URL for ``file_id``.

        Raises ``NotFoundException`` for missing / deleted / not-yet-uploaded
        files (cross-workspace identity is not leaked).

        Issue #1136: a context-bound file requires **read** access to its
        context (VIEWER+ via the context ACL) — so a workspace viewer can no
        longer download files scoped to a context they cannot see. A NULL
        context_id keeps the legacy workspace-viewer read path. A denied read is
        reported as ``NotFoundException`` (404), NOT ``AuthorizationError`` (403),
        so a workspace member cannot use the status code to enumerate which file
        ids exist behind a context they cannot access (existence-hiding, uniform
        with the cross-workspace / missing-file 404 from ``_load_file``).
        """
        file = await self._load_file(workspace_id, file_id)
        # Issue #1136: context-bound files require READ access. Any denial (no
        # actor, AuthorizationError, or a missing/soft-deleted context) collapses
        # to the uniform "not found" so file existence never leaks across a
        # context boundary on a read.
        if file.context_id is not None:
            try:
                if not actor_user_id:
                    raise AuthorizationError("Insufficient permissions")
                await PermissionService(self.db).check_context_access(
                    actor_user_id, file.context_id
                )
            except (AuthorizationError, NotFoundException) as exc:
                msg = f"file {file_id} not found in workspace {workspace_id}"
                raise NotFoundException(msg) from exc
        if file.status != "uploaded":
            msg = f"file {file_id} not found in workspace {workspace_id}"
            raise NotFoundException(msg)

        settings = get_settings()
        return await self._storage.generate_presigned_get(
            key=file.storage_key or "",
            filename=file.filename,
            ttl_seconds=settings.presign_get_ttl_seconds,
        )

    # ------------------------------------------------------------------
    # Legacy attachment migration (Commit 9 of #485)
    # ------------------------------------------------------------------

    async def migrate_attachment(
        self,
        *,
        workspace_id: UUID,
        attachment_id: UUID,
        filename: str,
        content_type: str,
        size_bytes: int,
        data: bytes,
        created_by: str,
    ) -> FileObject:
        """Copy a legacy ``attachments`` BYTEA row to R2 + ``file_objects``.

        Idempotent: if a ``file_objects`` row already exists for this
        attachment (matched by the legacy ``storage_key`` shape) the
        existing row is returned without re-uploading.

        The legacy ``Attachment`` row stays untouched — the existing
        REST attachments route continues to serve from BYTEA until a
        follow-up PR migrates it. This commit's job is to land the
        bytes in R2 and the metadata in ``file_objects`` so the
        cutover is unblocked.
        """
        import hashlib

        sha256 = hashlib.sha256(data).hexdigest()
        # Distinct key shape so legacy migrations are visually
        # distinguishable from new uploads (no sha256 sub-prefix here
        # because the attachment_id is already unique-per-workspace).
        storage_key = f"{workspace_id}/legacy/{attachment_id}/{filename}"

        existing = await self.db.execute(
            select(FileObject).where(FileObject.storage_key == storage_key)
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row is not None:
            logger.info(
                "attachment_migration_skipped_already_done",
                attachment_id=str(attachment_id),
                file_id=str(existing_row.id),
            )
            return existing_row

        await self._storage.write_object(
            key=storage_key,
            data=data,
            content_type=content_type,
            sha256=sha256,
        )

        now = utcnow()
        file = FileObject(
            id=uuid4(),
            workspace_id=workspace_id,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            filename=filename,
            storage_backend="r2",
            storage_key=storage_key,
            status="uploaded",
            expires_at=None,
            created_by=created_by,
            created_at=now,
            uploaded_at=now,
        )
        self.db.add(file)
        await self.db.flush()
        await self._upsert_workspace_usage(workspace_id, delta_bytes=size_bytes, delta_files=1)
        await self.db.commit()
        await self.db.refresh(file)

        # Bump the live Redis quota counter so concurrent ``reserve_upload``
        # calls don't see a stale-low total until the 24h reseed expires.
        # Wrapped in try/except — RedisError here is self-healing on next
        # reseed and MUST NOT roll back the migrated row.
        await storage_quota_service.bump_committed_storage_bytes(
            workspace_id=workspace_id,
            size_bytes=size_bytes,
        )

        logger.info(
            "attachment_migrated_to_r2",
            attachment_id=str(attachment_id),
            file_id=str(file.id),
            size_bytes=size_bytes,
        )
        return file

    # ------------------------------------------------------------------
    # Delete (R5: immediate quota release)
    # ------------------------------------------------------------------

    async def delete_file(
        self,
        *,
        workspace_id: UUID,
        file_id: UUID,
        actor_user_id: str | None = None,
    ) -> None:
        """Soft-delete and immediately release the quota.

        Issue #1136: deleting a context-bound file requires write access to its
        context. If the owning context is gone (soft-deleted → NotFoundException),
        fall back to the caller's workspace gate so files in a deleted context can
        still be cleaned up — otherwise their quota would be stuck until the
        workspace itself is deleted. An AuthorizationError on a *live* context is
        re-raised: a non-editor cannot delete a file scoped away from them.

        R5 contract: ``deleted_at`` is set and ``workspace_storage_usage``
        is decremented in one DB transaction. ``release_storage_bytes``
        runs **after** that commit succeeds — Redis follows the durable
        DB state, never leads it. If the commit fails, the row stays
        ``uploaded`` and Redis is unchanged, preserving the invariant
        that "Redis ≤ committed file_objects sum" (modulo in-flight
        reservations) until the next reseed.

        The R2 binary stays for 7 days (sweeper handles deletion).
        """
        file = await self._load_file(workspace_id, file_id)

        # Issue #1136: context-ACL gate (see docstring for the deleted-context
        # cleanup fallback). Fail-closed if no actor is supplied for a
        # context-bound file.
        if file.context_id is not None:
            if not actor_user_id:
                raise AuthorizationError("Insufficient permissions")
            try:
                await PermissionService(self.db).check_context_write(actor_user_id, file.context_id)
            except NotFoundException:
                pass  # owning context deleted → workspace gate (already enforced) suffices

        # Reserved rows: ``reserve_upload`` already incremented the Redis
        # counter, but no committed-bytes are tracked in
        # ``workspace_storage_usage`` yet. Release the reservation here
        # so a cancelled in-flight upload doesn't leave the workspace's
        # quota stuck until the orphan sweeper runs (15 minutes later
        # by default — long enough on the Free 100 MiB tier to block
        # the next legitimate upload after a single cancelled big one).
        #
        # Critically, ALSO transition the row to ``status='failed'`` so
        # the orphan sweeper's ``WHERE status='reserved'`` filter
        # excludes it — without this, the sweeper would later call
        # ``release_storage_bytes`` a second time on the same row and
        # under-count the workspace's Redis counter.
        if file.status == "reserved":
            reserved_size = file.size_bytes
            file.deleted_at = utcnow()
            file.status = "failed"
            await self.db.commit()
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=reserved_size,
            )
            logger.info(
                "file_reserved_cancelled",
                workspace_id=str(workspace_id),
                file_id=str(file_id),
                size_bytes=reserved_size,
            )
            return

        # Failed rows: orphan sweeper already released the Redis counter
        # when the row transitioned reserved → failed. Just mark deleted.
        if file.status != "uploaded":
            file.deleted_at = utcnow()
            await self.db.commit()
            return

        size = file.size_bytes
        file.deleted_at = utcnow()
        await self._upsert_workspace_usage(
            workspace_id,
            delta_bytes=-size,
            delta_files=-1,
        )
        await self.db.commit()

        await storage_quota_service.release_storage_bytes(
            workspace_id=workspace_id,
            size_bytes=size,
        )
        logger.info(
            "file_soft_deleted",
            workspace_id=str(workspace_id),
            file_id=str(file_id),
            size_bytes=size,
        )

    async def purge_files_for_contexts(self, context_ids: Sequence[UUID]) -> dict[UUID, int]:
        """Soft-delete every live, non-reserved file bound to ``context_ids``.

        Admin user-erasure hard-deletes the target's contexts, which would
        fire ``file_objects.context_id``'s ``ON DELETE SET NULL`` and silently
        widen a private context's files to workspace scope (any viewer could
        then download them). Call this in the same transaction, *before* the
        contexts are deleted, so the files go away with their ACL boundary.

        ``reserved`` rows are left for the orphan sweeper: they are not
        downloadable (``get_presigned_download`` requires ``uploaded``) and
        the sweeper owns their Redis release — marking them ``failed`` here
        would make it skip them and leak the reservation.

        Does NOT commit (runs inside the caller's transaction). Returns the
        released uploaded-bytes per workspace so the caller can call
        ``storage_quota_service.release_storage_bytes`` for each surviving
        workspace AFTER its commit succeeds (Redis follows the DB, R5).
        """
        if not context_ids:
            return {}
        result = await self.db.execute(
            select(FileObject).where(
                FileObject.context_id.in_(list(context_ids)),
                FileObject.deleted_at.is_(None),
                FileObject.status != "reserved",
            )
        )
        files = list(result.scalars().all())
        released: dict[UUID, int] = {}
        now = utcnow()
        for file in files:
            file.deleted_at = now
            if file.status != "uploaded":
                continue  # failed rows: Redis already released by the sweeper
            await self._upsert_workspace_usage(
                file.workspace_id,
                delta_bytes=-file.size_bytes,
                delta_files=-1,
            )
            released[file.workspace_id] = released.get(file.workspace_id, 0) + file.size_bytes
        if files:
            logger.info(
                "context_bound_files_purged",
                context_count=len(set(context_ids)),
                file_count=len(files),
                workspaces=[str(w) for w in released],
            )
        return released

    # ------------------------------------------------------------------
    # Counter helper (UPSERT pattern)
    # ------------------------------------------------------------------

    async def _upsert_workspace_usage(
        self,
        workspace_id: UUID,
        *,
        delta_bytes: int,
        delta_files: int,
    ) -> None:
        """Atomically adjust ``workspace_storage_usage`` for the given
        workspace.

        ON CONFLICT updates the existing row; otherwise inserts a fresh
        row clamped to the non-negative regime (the CHECK constraint
        also enforces this server-side).
        """
        now = utcnow()
        stmt = (
            pg_insert(WorkspaceStorageUsage)
            .values(
                workspace_id=workspace_id,
                used_bytes=max(delta_bytes, 0),
                file_count=max(delta_files, 0),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[WorkspaceStorageUsage.workspace_id],
                set_={
                    "used_bytes": WorkspaceStorageUsage.used_bytes + delta_bytes,
                    "file_count": WorkspaceStorageUsage.file_count + delta_files,
                    "updated_at": now,
                },
            )
        )
        await self.db.execute(stmt)
