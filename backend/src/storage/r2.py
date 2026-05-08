"""Cloudflare R2 implementation of ``BlobStorageProtocol`` (Issue #485).

R2 exposes an S3-compatible API, so we use ``aioboto3`` against R2's
endpoint. Single ``aioboto3.Session`` is reused; the per-call client
context manager opens a connection from the underlying ``aiobotocore``
pool, so there is no per-request socket setup cost.
"""

from __future__ import annotations

import base64
from typing import Any

import aioboto3
import structlog
from botocore.exceptions import ClientError

from storage.protocol import ObjectMetadata
from utils.exceptions import ExternalServiceError

logger = structlog.get_logger(__name__)


class R2Storage:
    """``BlobStorageProtocol`` impl backed by Cloudflare R2.

    The class deliberately does not inherit from ``BlobStorageProtocol``
    — runtime structural typing (``@runtime_checkable``) is enough, and
    keeping it separate avoids tight coupling for Phase 2 BYO impls.
    """

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        endpoint_url: str,
        enable_checksum_binding: bool = False,
    ) -> None:
        if not (account_id and access_key_id and secret_access_key and bucket and endpoint_url):
            raise ValueError(
                "R2Storage requires non-empty account_id, access_key_id, "
                "secret_access_key, bucket, and endpoint_url"
            )
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._enable_checksum_binding = enable_checksum_binding
        self._session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",  # R2 ignores region; "auto" is the canonical value
        )

    def _client(self) -> Any:
        """Return a fresh aioboto3 S3 client context manager.

        Callers MUST use it with ``async with`` so the underlying
        connection is returned to the pool.
        """
        return self._session.client("s3", endpoint_url=self._endpoint_url)

    async def write_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
        sha256: str,
    ) -> None:
        """Server-side upload (used by the attachments migration only)."""
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": sha256},
            )
            logger.info("r2_object_written", key=key, size_bytes=len(data))

    async def head_object(self, key: str) -> ObjectMetadata | None:
        async with self._client() as client:
            try:
                resp = await client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                # Both shapes appear in the wild — botocore translates
                # HTTP 404 to ``"404"`` for HEAD; ``NoSuchKey`` is the
                # GET shape, kept for safety in case R2 differs.
                if code in ("404", "NoSuchKey"):
                    return None
                # Non-404 errors (5xx, AccessDenied, throttling, …) are
                # surfaced as a domain exception so REST/MCP layers map
                # them to a clear 502 instead of a raw boto traceback.
                logger.warning(
                    "r2_head_object_error",
                    key=key,
                    code=code,
                    error=str(exc),
                )
                raise ExternalServiceError(
                    "R2",
                    f"head_object failed for key={key!r}: {code}",
                ) from exc
            return ObjectMetadata(
                size_bytes=int(resp["ContentLength"]),
                etag=str(resp.get("ETag", "")).strip('"'),
            )

    async def delete_object(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)
            logger.info("r2_object_deleted", key=key)

    async def generate_presigned_put(
        self,
        key: str,
        content_type: str,
        size_bytes: int,
        ttl_seconds: int,
        sha256: str,
    ) -> str:
        # Issue #556 originally proposed presigned POST (signs body sha256
        # via policy condition); R2 returns 501 NotImplemented for POST.
        # We sign ``ChecksumSHA256`` on PUT instead — see the live spike
        # at backend/tests/integration/test_r2_live.py for the contract.
        # The flag is gated by ``r2_checksum_binding_enabled`` (default
        # False) so backend can deploy ahead of SDK rollout.
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
        }
        if self._enable_checksum_binding:
            params["ChecksumSHA256"] = base64.b64encode(bytes.fromhex(sha256)).decode()
        async with self._client() as client:
            url = await client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=ttl_seconds,
                HttpMethod="PUT",
            )
            return str(url)

    async def generate_presigned_get(
        self,
        key: str,
        filename: str,
        ttl_seconds: int,
    ) -> str:
        # Sanitize the filename before embedding into the
        # ``Content-Disposition`` header. Without this a filename
        # containing a literal ``"`` could break out of the quoted
        # parameter and enable filename-spoofing for downloads;
        # CR/LF could (in principle) corrupt the header
        # (CSO Gate2 finding F-1 + Copilot loop 5 finding on PR #551).
        safe_filename = (
            filename.replace('"', "_").replace("\n", "_").replace("\r", "_").replace("\x00", "_")
        )
        async with self._client() as client:
            url = await client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{safe_filename}"',
                },
                ExpiresIn=ttl_seconds,
                HttpMethod="GET",
            )
            return str(url)

    async def close(self) -> None:
        """Release any pooled resources held by aiobotocore.

        Currently a no-op — aioboto3 pools live inside the per-call
        ``async with`` block and are released on context exit. Kept on
        the API surface so the lifespan handler can call it
        unconditionally and a future eager-init refactor stays
        backwards compatible.
        """
        return None
