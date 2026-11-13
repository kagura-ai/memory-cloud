"""The stdlib clamd INSTREAM client (#565), against a fake clamd socket server.

What a verdict may be trusted for: only a complete stream answered with
exactly ``stream: OK`` is clean. A ``FOUND`` is a finding; clamd's own limit
findings (``Heuristics.Limits.Exceeded*``, ``Heuristics.ScanTimeout``) mean
"not scanned", never clean. A failure before any byte was streamed is
``ClamdUnavailable`` (the scanner is down: it does not count against the
file); anything after streaming started is ``ClamdScanFailed``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from services.clamd_client import (
    CHUNK_BYTES,
    REPLY_LIMIT_BYTES,
    ClamdScanFailed,
    ClamdSizeLimit,
    ClamdUnavailable,
    ClamdVerdict,
    clamd_instream,
    clamd_ping,
    clamd_version,
    parse_instream_reply,
    sanitize_signature,
)
from tests.services._fake_clamd import (
    EICAR,
    EICAR_SIGNATURE,
    FakeClamd,
    free_port,
    running_fake_clamd,
)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _scan(port: int, *parts: bytes, expected: int | None = None, **kw) -> ClamdVerdict:
    total = sum(len(p) for p in parts) if expected is None else expected
    return await clamd_instream(
        "127.0.0.1",
        port,
        _chunks(*parts),
        expected_bytes=total,
        connect_timeout=kw.get("connect_timeout", 2.0),
        scan_timeout=kw.get("scan_timeout", 5.0),
    )


class TestInstream:
    async def test_clean_stream(self):
        async with running_fake_clamd() as (fake, port):
            verdict = await _scan(port, b"hello ", b"world")
        assert verdict == ClamdVerdict("clean")
        assert fake.streams == [b"hello world"]
        assert fake.terminated == [True]
        assert fake.commands == [b"zINSTREAM\0"]

    async def test_eicar_is_found(self):
        async with running_fake_clamd() as (_, port):
            verdict = await _scan(port, b"prefix ", EICAR, b" suffix")
        assert verdict == ClamdVerdict("infected", EICAR_SIGNATURE)

    async def test_eicar_split_across_chunk_boundaries(self):
        async with running_fake_clamd() as (fake, port):
            verdict = await _scan(port, EICAR[:10], EICAR[10:30], EICAR[30:])
        assert verdict.outcome == "infected"
        assert fake.streams == [EICAR]

    async def test_large_chunks_are_split_to_the_wire_chunk_size(self):
        payload = b"a" * (CHUNK_BYTES * 2 + 5)
        async with running_fake_clamd() as (fake, port):
            verdict = await _scan(port, payload)
        assert verdict.outcome == "clean"
        assert fake.streams == [payload]

    async def test_empty_file_is_scanned(self):
        async with running_fake_clamd() as (fake, port):
            verdict = await _scan(port)
        assert verdict.outcome == "clean"
        assert fake.terminated == [True]

    @pytest.mark.parametrize(
        "name",
        [
            "Heuristics.Limits.Exceeded.MaxFileSize",
            "Heuristics.Limits.Exceeded.MaxRecursion",
            "Heuristics.ScanTimeout",
        ],
    )
    async def test_limit_findings_are_unscannable_not_clean(self, name):
        async with running_fake_clamd(FakeClamd(reply=f"stream: {name} FOUND\0".encode())) as (
            _,
            port,
        ):
            verdict = await _scan(port, b"zip bomb")
        assert verdict == ClamdVerdict("unscannable", name)

    async def test_size_limit_refusal(self):
        """clamd answers "INSTREAM size limit exceeded" and closes (StreamMaxLength)."""
        fake = FakeClamd(stream_max_bytes=CHUNK_BYTES)
        async with running_fake_clamd(fake) as (_, port):
            with pytest.raises(ClamdSizeLimit):
                await _scan(port, b"x" * (CHUNK_BYTES * 2))

    async def test_size_limit_refusal_while_still_sending_is_a_scan_failure(self):
        """The reply can be lost under the reset; either way it is never clean."""
        fake = FakeClamd(stream_max_bytes=CHUNK_BYTES)
        async with running_fake_clamd(fake) as (_, port):
            with pytest.raises(ClamdScanFailed):
                await _scan(port, *([b"x" * CHUNK_BYTES] * 256))

    async def test_short_source_never_sends_the_terminator(self):
        """A truncated storage read must not be scanned as a prefix (R6)."""
        async with running_fake_clamd() as (fake, port):
            with pytest.raises(ClamdScanFailed, match="ended after 5 of 10 bytes"):
                await _scan(port, b"12345", expected=10)
            await asyncio.sleep(0.05)
        assert fake.terminated == [False]

    async def test_long_source_is_refused(self):
        async with running_fake_clamd() as (fake, port):
            with pytest.raises(ClamdScanFailed, match="longer than"):
                await _scan(port, b"1234567890", expected=4)
            await asyncio.sleep(0.05)
        assert fake.terminated == [False]

    async def test_source_error_propagates_without_terminator(self):
        async def broken() -> AsyncIterator[bytes]:
            yield b"abc"
            raise OSError("storage read failed")

        async with running_fake_clamd() as (fake, port):
            with pytest.raises(OSError, match="storage read failed"):
                await clamd_instream(
                    "127.0.0.1",
                    port,
                    broken(),
                    expected_bytes=10,
                    connect_timeout=2.0,
                    scan_timeout=5.0,
                )
            await asyncio.sleep(0.05)
        assert fake.terminated == [False]

    async def test_connect_refused_is_unavailable(self):
        port = await free_port()
        with pytest.raises(ClamdUnavailable):
            await _scan(port, b"abc")

    async def test_reset_mid_stream_counts_as_a_scan_failure(self):
        fake = FakeClamd(reset_after_bytes=1)
        async with running_fake_clamd(fake) as (_, port):
            with pytest.raises(ClamdScanFailed):
                await _scan(port, *([b"y" * CHUNK_BYTES] * 64))

    async def test_slow_scanner_times_out_as_a_scan_failure(self):
        async with running_fake_clamd(FakeClamd(delay=2.0)) as (_, port):
            with pytest.raises(ClamdScanFailed, match="timed out"):
                await _scan(port, b"abc", scan_timeout=0.2)

    @pytest.mark.parametrize(
        "reply",
        [
            b"stream: OK",  # no NUL terminator, then EOF
            b"\0",
            b"stream: ok\0",
            b"stream: OKAY\0",
            b"stream: OK\nstream: OK\0",
            b"OK\0",
            b"stream: Something ERROR\0",
            b"stream:  FOUND\0",
        ],
    )
    async def test_anything_but_an_exact_reply_is_a_failure(self, reply):
        async with running_fake_clamd(FakeClamd(reply=reply)) as (_, port):
            with pytest.raises(ClamdScanFailed):
                await _scan(port, b"abc")

    async def test_oversized_reply_is_cut_off(self):
        reply = b"stream: " + b"A" * (REPLY_LIMIT_BYTES * 2) + b" FOUND\0"
        async with running_fake_clamd(FakeClamd(reply=reply)) as (_, port):
            with pytest.raises(ClamdScanFailed, match="reply"):
                await _scan(port, b"abc")

    async def test_signature_is_sanitized(self):
        reply = b"stream: Win.Evil\x1b[31m\r\nInjected FOUND\0"
        async with running_fake_clamd(FakeClamd(reply=reply)) as (_, port):
            verdict = await _scan(port, b"abc")
        assert verdict.outcome == "infected"
        assert verdict.signature is not None
        assert "\x1b" not in verdict.signature and "\n" not in verdict.signature
        assert verdict.signature.startswith("Win.Evil")


class TestParseReply:
    def test_exact_ok(self):
        assert parse_instream_reply(b"stream: OK\0") == ClamdVerdict("clean")

    def test_found(self):
        assert parse_instream_reply(b"stream: Win.Test.EICAR_HDB-1 FOUND\0") == ClamdVerdict(
            "infected", "Win.Test.EICAR_HDB-1"
        )

    def test_size_limit_error(self):
        with pytest.raises(ClamdSizeLimit):
            parse_instream_reply(b"INSTREAM size limit exceeded. ERROR\0")

    def test_other_error(self):
        with pytest.raises(ClamdScanFailed):
            parse_instream_reply(b"stream: Can't allocate memory ERROR\0")


class TestSanitize:
    def test_caps_length_and_strips_controls(self):
        name = "A" * 300 + "\x00\x07"
        cleaned = sanitize_signature(name)
        assert len(cleaned) == 255
        assert set(cleaned) == {"A"}

    def test_replaces_non_ascii(self):
        assert sanitize_signature("Bad‮Name") == "Bad?Name"


class TestPingVersion:
    async def test_ping(self):
        async with running_fake_clamd() as (_, port):
            assert await clamd_ping("127.0.0.1", port, timeout=2.0) is True

    async def test_ping_wrong_reply(self):
        async with running_fake_clamd(FakeClamd(ping_reply=b"PANG\0")) as (_, port):
            assert await clamd_ping("127.0.0.1", port, timeout=2.0) is False

    async def test_ping_down(self):
        assert await clamd_ping("127.0.0.1", await free_port(), timeout=1.0) is False

    async def test_ping_slow(self):
        async with running_fake_clamd(FakeClamd(delay=2.0)) as (_, port):
            assert await clamd_ping("127.0.0.1", port, timeout=0.2) is False

    async def test_version(self):
        async with running_fake_clamd() as (_, port):
            version = await clamd_version("127.0.0.1", port, timeout=2.0)
        assert version is not None and version.startswith("ClamAV 1.4.3/")

    async def test_version_down(self):
        assert await clamd_version("127.0.0.1", await free_port(), timeout=1.0) is None
