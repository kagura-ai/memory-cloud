"""Integration test for the S3-compatible backend against MinIO (Issue #994).

Proves the SAME ``S3CompatibleStorage`` code that serves Cloudflare R2 in the
managed cloud also works against a self-hosted MinIO endpoint — the upload /
download acceptance criterion for #994 (BYO storage). Mirrors the live-R2
contract test (``test_r2_live.py``) but against MinIO, which CI starts as a
local container.

Opt-in / gated: skipped unless ``MINIO_TEST_ENDPOINT`` (and the access/secret/
bucket vars) are set AND the endpoint is reachable. Locally:

    docker run -d -p 9000:9000 -e MINIO_ROOT_USER=minioadmin \
      -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data
    MINIO_TEST_ENDPOINT=http://localhost:9000 MINIO_TEST_ACCESS_KEY=minioadmin \
    MINIO_TEST_SECRET_KEY=minioadmin MINIO_TEST_BUCKET=kagura-files-test \
    pytest backend/tests/integration/test_minio_integration.py -v

CI wires these in the ``backend-integration`` job (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from urllib.error import URLError
from urllib.request import urlopen

import httpx
import pytest

from storage.s3_compatible import S3CompatibleStorage

_ENDPOINT = os.getenv("MINIO_TEST_ENDPOINT", "")
_ACCESS_KEY = os.getenv("MINIO_TEST_ACCESS_KEY", "")
_SECRET_KEY = os.getenv("MINIO_TEST_SECRET_KEY", "")
_BUCKET = os.getenv("MINIO_TEST_BUCKET", "kagura-files-test")


def _minio_reachable() -> bool:
    if not (_ENDPOINT and _ACCESS_KEY and _SECRET_KEY):
        return False
    try:
        # MinIO liveness endpoint returns 200 when the server is up.
        with urlopen(f"{_ENDPOINT}/minio/health/live", timeout=3) as resp:  # noqa: S310
            return resp.status == 200
    except (URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _minio_reachable(),
    reason=(
        "MinIO integration test skipped — set MINIO_TEST_ENDPOINT / "
        "MINIO_TEST_ACCESS_KEY / MINIO_TEST_SECRET_KEY (and start a reachable "
        "MinIO at that endpoint)."
    ),
)


def _hex_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def storage() -> S3CompatibleStorage:
    # checksum binding off — the #556 ChecksumSHA256 integrity gate is an
    # R2-specific server feature; this test exercises basic S3 upload/download.
    return S3CompatibleStorage(
        account_id="minio",
        access_key_id=_ACCESS_KEY,
        secret_access_key=_SECRET_KEY,
        bucket=_BUCKET,
        endpoint_url=_ENDPOINT,
        enable_checksum_binding=False,
    )


@pytest.fixture(scope="module", autouse=True)
async def _ensure_bucket(storage: S3CompatibleStorage):
    """Create the test bucket if MinIO doesn't have it yet (fresh container)."""
    from botocore.exceptions import ClientError

    async with storage._client() as client:
        try:
            await client.create_bucket(Bucket=_BUCKET)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # Already exists (ours or anyone's) is fine for the test.
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
    yield


@pytest.fixture
def run_prefix() -> str:
    return f"_spike/test_minio/{uuid.uuid4().hex}"


class TestMinioRoundTrip:
    @pytest.mark.asyncio
    async def test_write_head_get_delete_roundtrip(
        self, storage: S3CompatibleStorage, run_prefix: str
    ) -> None:
        """Server-side write → head (size/etag) → presigned GET → bytes match → delete."""
        key = f"{run_prefix}/roundtrip.txt"
        data = b"minio-s3-compatible-roundtrip"
        try:
            await storage.write_object(
                key=key,
                data=data,
                content_type="text/plain",
                sha256=_hex_sha256(data),
            )

            meta = await storage.head_object(key)
            assert meta is not None
            assert meta["size_bytes"] == len(data)
            assert meta["etag"]

            get_url = await storage.generate_presigned_get(
                key=key, filename="roundtrip.txt", ttl_seconds=300
            )
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.get(get_url)
            assert resp.status_code == 200
            assert resp.content == data
        finally:
            await storage.delete_object(key)

        assert await storage.head_object(key) is None

    @pytest.mark.asyncio
    async def test_presigned_put_upload_lands(
        self, storage: S3CompatibleStorage, run_prefix: str
    ) -> None:
        """Presigned PUT → client uploads via the URL → head confirms it landed."""
        key = f"{run_prefix}/presigned_put.bin"
        data = b"uploaded-via-presigned-put-url"
        try:
            put_url = await storage.generate_presigned_put(
                key=key,
                content_type="application/octet-stream",
                size_bytes=len(data),
                ttl_seconds=300,
                sha256=_hex_sha256(data),
            )
            async with httpx.AsyncClient(timeout=30.0) as http:
                put_resp = await http.put(
                    put_url,
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
            assert put_resp.status_code in (200, 204), put_resp.text

            meta = await storage.head_object(key)
            assert meta is not None
            assert meta["size_bytes"] == len(data)
        finally:
            await storage.delete_object(key)
