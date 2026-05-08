"""Integration tests for the live R2 contract (Issue #485 + #556).

Exercises the full ``BlobStorageProtocol`` surface against a real
Cloudflare R2 bucket. Skipped by default — set ``R2_LIVE_TEST=1`` and
export the five R2_* env vars (``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``,
``R2_SECRET_ACCESS_KEY``, ``R2_BUCKET``, ``R2_ENDPOINT_URL``) to opt in:

    R2_LIVE_TEST=1 \
    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
    R2_BUCKET=kagura-memory-files-dev R2_ENDPOINT_URL=https://...r2.cloudflarestorage.com \
    pytest backend/tests/integration/test_r2_live.py -v

The unit test counterpart at ``backend/tests/storage/test_r2.py`` pins
``R2Storage`` construction. This file pins the live wire contract.

Issue #556 specifically: ``TestGeneratePresignedPut`` verifies that R2
honors the ``ChecksumSHA256`` server-side body sha256 binding —
declaring sha=X but uploading bytes whose digest is Y must be rejected
by R2 with HTTP 400 BadDigest, not silently accepted. The original
``generate_presigned_post`` proposal turned out to be non-viable on R2
(returns 501 NotImplemented) — the alternative tested here is the
production approach.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid

import httpx
import pytest

from storage.r2 import R2Storage

_R2_LIVE = os.getenv("R2_LIVE_TEST") == "1"
_REQUIRED_VARS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_ENDPOINT_URL",
)
_CREDS_PRESENT = all(os.getenv(k) for k in _REQUIRED_VARS)

pytestmark = pytest.mark.skipif(
    not (_R2_LIVE and _CREDS_PRESENT),
    reason=(
        "Live R2 test skipped — set R2_LIVE_TEST=1 and export R2_* env vars: "
        + ", ".join(_REQUIRED_VARS)
    ),
)


@pytest.fixture(scope="module")
def storage() -> R2Storage:
    """Real ``R2Storage`` bound to the bucket selected by env.

    Constructed with ``enable_checksum_binding=True`` so the
    ``TestGeneratePresignedPut`` class actually exercises the integrity
    gate (settings default is False for staged rollout — see
    ``r2_checksum_binding_enabled`` setting docstring).
    """
    return R2Storage(
        account_id=os.environ["R2_ACCOUNT_ID"],
        access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        bucket=os.environ["R2_BUCKET"],
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        enable_checksum_binding=True,
    )


@pytest.fixture(scope="module")
def run_prefix() -> str:
    """Unique prefix per test-run so concurrent runs and crash leftovers do not collide.

    Cleanup uses try/finally inside individual tests; this prefix is the
    safety net so a future op can sweep stale `_spike/test_r2_live/...`
    keys via R2 lifecycle policy without affecting production data.
    """
    return f"_spike/test_r2_live/{uuid.uuid4().hex}"


def _hex_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(hex_sha: str) -> str:
    """hex digest → raw 32 bytes → base64 (the form R2 expects in
    ``ChecksumSHA256`` and the ``x-amz-checksum-sha256`` header)."""
    return base64.b64encode(bytes.fromhex(hex_sha)).decode()


class TestWriteObjectAndHeadObject:
    """Server-side write + ``head_object`` semantics on real R2."""

    @pytest.mark.asyncio
    async def test_write_then_head_returns_size_and_etag(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        key = f"{run_prefix}/write_then_head.bin"
        data = b"hello-r2-live-test"
        try:
            await storage.write_object(
                key=key,
                data=data,
                content_type="application/octet-stream",
                sha256=_hex_sha256(data),
            )

            meta = await storage.head_object(key)
            assert meta is not None
            assert meta["size_bytes"] == len(data)
            assert meta["etag"]
        finally:
            await storage.delete_object(key)

    @pytest.mark.asyncio
    async def test_head_object_missing_returns_none(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        key = f"{run_prefix}/never-uploaded-{uuid.uuid4().hex}"
        meta = await storage.head_object(key)
        assert meta is None


class TestDeleteObject:
    @pytest.mark.asyncio
    async def test_delete_existing_object_removes_it(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        key = f"{run_prefix}/delete_target.bin"
        data = b"to-be-deleted"
        await storage.write_object(
            key=key,
            data=data,
            content_type="application/octet-stream",
            sha256=_hex_sha256(data),
        )

        assert await storage.head_object(key) is not None
        await storage.delete_object(key)
        assert await storage.head_object(key) is None


class TestGeneratePresignedPut:
    """Issue #556: ``ChecksumSHA256`` server-side body binding.

    Three cases mirror the throwaway spike that originally validated
    this approach against the dev bucket. Promoted here with a unique-
    prefix harness so they're stable to re-run.

    Background: the issue originally proposed
    ``generate_presigned_post`` with a POST policy condition on
    ``$x-amz-content-sha256``. R2 returns HTTP 501 NotImplemented for
    presigned POST — see the docstring of this module. ``ChecksumSHA256``
    on presigned PUT is the working alternative.
    """

    @pytest.mark.asyncio
    async def test_positive_match_uploads_successfully(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        """D1: declared sha=X, header X, body X bytes → HTTP 200."""
        bytes_x = b"the_content_we_declared_X_aaaaaaaaaaaaa"
        sha_x = _hex_sha256(bytes_x)
        key = f"{run_prefix}/D1_positive.bin"
        try:
            url = await storage.generate_presigned_put(
                key=key,
                content_type="application/octet-stream",
                size_bytes=len(bytes_x),
                ttl_seconds=300,
                sha256=sha_x,
            )

            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.put(
                    url,
                    content=bytes_x,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "x-amz-checksum-sha256": _b64(sha_x),
                    },
                )
            assert resp.status_code == 200, resp.text

            meta = await storage.head_object(key)
            assert meta is not None
            assert meta["size_bytes"] == len(bytes_x)
        finally:
            await storage.delete_object(key)

    @pytest.mark.asyncio
    async def test_mismatch_rejected_with_bad_digest(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        """D2: declared sha=X, header X, body Y bytes → HTTP 400 BadDigest.

        This is the integrity gate the issue exists to install. If R2
        ever accepts this case, the dedup-poisoning gap from #485
        Phase 1 re-opens silently — keep this test as a regression
        guard against silent backend behavior drift.
        """
        bytes_x = b"declared_content_X_aaaaaaaaaaaaaaaaaaaaa"
        bytes_y = b"actual_content_YYY_zzzzzzzzzzzzzzzzzzzzz"
        assert len(bytes_x) == len(bytes_y), "equal-length isolates sha mismatch from size mismatch"
        sha_x = _hex_sha256(bytes_x)
        key = f"{run_prefix}/D2_mismatch.bin"

        # ``size_bytes`` is what the caller declared at reservation time —
        # decoupled from the body length so a refactor that breaks the
        # equal-length invariant won't silently switch the failure mode.
        url = await storage.generate_presigned_put(
            key=key,
            content_type="application/octet-stream",
            size_bytes=len(bytes_x),
            ttl_seconds=300,
            sha256=sha_x,
        )

        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.put(
                url,
                content=bytes_y,
                headers={
                    "Content-Type": "application/octet-stream",
                    "x-amz-checksum-sha256": _b64(sha_x),
                },
            )
        assert resp.status_code == 400, resp.text
        assert "BadDigest" in resp.text

        # The object MUST NOT have been persisted on rejection.
        assert await storage.head_object(key) is None

    @pytest.mark.asyncio
    async def test_tampered_header_rejected_with_signature_mismatch(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        """D3: presigned URL signed for sha=X, client sends header sha=Y → 403.

        The checksum is part of the SigV4 signature, so a client that
        swaps ``x-amz-checksum-sha256`` to match its own (different)
        bytes invalidates the signature. Without this property an
        attacker holding the presigned URL could substitute their own
        bytes by also recomputing the header.
        """
        bytes_x = b"declared_content_X_aaaaaaaaaaaaaaaaaaaaa"
        bytes_y = b"actual_content_YYY_zzzzzzzzzzzzzzzzzzzzz"
        sha_x = _hex_sha256(bytes_x)
        sha_y = _hex_sha256(bytes_y)
        key = f"{run_prefix}/D3_tampered.bin"

        url = await storage.generate_presigned_put(
            key=key,
            content_type="application/octet-stream",
            size_bytes=len(bytes_x),
            ttl_seconds=300,
            sha256=sha_x,
        )

        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.put(
                url,
                content=bytes_y,
                headers={
                    "Content-Type": "application/octet-stream",
                    "x-amz-checksum-sha256": _b64(sha_y),
                },
            )
        # R2 validates the SigV4 signature before evaluating body integrity,
        # so a mismatched ``x-amz-checksum-sha256`` header invalidates the
        # signature → 403 (not 400 BadDigest). If R2 ever flips the
        # evaluation order this assertion would start seeing 400 — investigate
        # before loosening, since that would mean the body was at least
        # partially read by R2 before the auth check.
        assert resp.status_code == 403, resp.text
        assert "SignatureDoesNotMatch" in resp.text
        assert await storage.head_object(key) is None


class TestGeneratePresignedGet:
    @pytest.mark.asyncio
    async def test_round_trip_returns_bytes_with_filename_disposition(
        self, storage: R2Storage, run_prefix: str
    ) -> None:
        """``write_object`` → ``generate_presigned_get`` → http GET → bytes match.

        Also confirms the ``Content-Disposition`` filename is set so
        browsers download with the original name instead of the
        storage key.
        """
        key = f"{run_prefix}/get_roundtrip.txt"
        data = b"presigned-get-roundtrip-bytes"
        try:
            await storage.write_object(
                key=key,
                data=data,
                content_type="text/plain",
                sha256=_hex_sha256(data),
            )

            url = await storage.generate_presigned_get(
                key=key, filename="report.txt", ttl_seconds=60
            )

            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.get(url)
            assert resp.status_code == 200
            assert resp.content == data
            cd = resp.headers.get("content-disposition", "")
            assert "report.txt" in cd
        finally:
            await storage.delete_object(key)
