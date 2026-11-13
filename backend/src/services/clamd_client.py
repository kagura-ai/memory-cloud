"""Minimal clamd client over TCP: PING, VERSION and INSTREAM (#565).

Standard library only (``asyncio`` + ``struct``). The protocol is clamd(8):
a command is ``z<NAME>\\0`` (NUL-terminated replies), ``INSTREAM`` is
followed by chunks of ``<4-byte big-endian length><data>`` and ends with a
zero-length chunk; the reply is ``stream: OK``, ``stream: <name> FOUND`` or
``<text> ERROR``.

What a verdict is trusted for:

- Only a stream of exactly ``expected_bytes`` bytes is ended with the
  terminator. A short or long source aborts the connection without it, so
  clamd never scans (and never answers ``OK`` for) a prefix.
- Only the exact reply ``stream: OK`` is clean. ``FOUND`` is a finding;
  names starting with ``Heuristics.Limits.Exceeded`` / ``Heuristics.ScanTimeout``
  are clamd saying it did NOT scan everything (``AlertExceedsMax yes``), which
  is reported as ``unscannable``, never as clean. Anything else, an empty reply
  or a reply without its NUL terminator is a failure.
- Failures before any payload byte was sent (connection refused, connect
  timeout, the command write failing) raise :class:`ClamdUnavailable`: the
  scanner is down, the file is not at fault. Failures after streaming started
  (reset, scan timeout, malformed reply, an ``ERROR`` reply) raise
  :class:`ClamdScanFailed`. Errors raised by the chunk source itself (a
  storage read) propagate unchanged.

clamd's TCP socket has no encryption or authentication: it must be reachable
only on the same host or a private network.
"""

from __future__ import annotations

import asyncio
import re
import struct
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Literal

#: Payload bytes per INSTREAM chunk on the wire.
CHUNK_BYTES = 64 * 1024
#: Longest reply read from clamd; anything longer is a failure.
REPLY_LIMIT_BYTES = 4096
#: Longest signature name kept (also the ``scan_signature`` column width).
SIGNATURE_MAX_CHARS = 255
#: Finding names clamd uses when it stopped scanning at a configured limit.
UNSCANNABLE_PREFIXES = ("Heuristics.Limits.Exceeded", "Heuristics.ScanTimeout")

_TERMINATOR = struct.pack("!I", 0)
_FOUND_RE = re.compile(r"^stream: (\S(?:.*\S)?) FOUND$", re.DOTALL)
_NOT_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")


@dataclass(frozen=True)
class ClamdVerdict:
    """What clamd said about one complete stream."""

    outcome: Literal["clean", "infected", "unscannable"]
    #: The sanitized finding name (never sent to clients).
    signature: str | None = None


class ClamdError(Exception):
    """Base class of the client's own errors."""


class ClamdUnavailable(ClamdError):
    """clamd could not be reached; nothing was streamed."""


class ClamdScanFailed(ClamdError):
    """The scan started but gave no usable verdict."""


class ClamdSizeLimit(ClamdScanFailed):
    """clamd refused the stream as longer than its ``StreamMaxLength``."""


def sanitize_signature(name: str) -> str:
    """Printable ASCII only, capped: a finding name goes into logs and the DB."""
    return _NOT_PRINTABLE_RE.sub("?", name.replace("\x00", ""))[:SIGNATURE_MAX_CHARS]


def parse_instream_reply(raw: bytes) -> ClamdVerdict:
    """Classify one NUL-terminated INSTREAM reply (strict).

    Raises:
        ClamdSizeLimit: ``INSTREAM size limit exceeded``.
        ClamdScanFailed: anything that is not exactly ``stream: OK`` or
            ``stream: <name> FOUND``.
    """
    if not raw.endswith(b"\0") or b"\0" in raw[:-1]:
        raise ClamdScanFailed("clamd reply was not one NUL-terminated line")
    text = raw[:-1].decode("utf-8", errors="replace")
    if text == "stream: OK":
        return ClamdVerdict("clean")
    found = _FOUND_RE.match(text)
    if found is not None:
        signature = sanitize_signature(found.group(1))
        if signature.startswith(UNSCANNABLE_PREFIXES):
            return ClamdVerdict("unscannable", signature)
        return ClamdVerdict("infected", signature)
    if "size limit exceeded" in text and text.endswith("ERROR"):
        raise ClamdSizeLimit("clamd refused the stream size (StreamMaxLength)")
    raise ClamdScanFailed(f"unexpected clamd reply: {sanitize_signature(text)[:80]!r}")


async def _read_reply(reader: asyncio.StreamReader) -> bytes:
    """Read up to and including the first NUL, at most ``REPLY_LIMIT_BYTES``."""
    buf = bytearray()
    while b"\0" not in buf:
        if len(buf) > REPLY_LIMIT_BYTES:
            raise ClamdScanFailed("clamd reply longer than the limit")
        chunk = await reader.read(REPLY_LIMIT_BYTES + 1 - len(buf))
        if not chunk:
            break  # EOF: the parser rejects a reply without its NUL
        buf += chunk
    if len(buf) > REPLY_LIMIT_BYTES:
        raise ClamdScanFailed("clamd reply longer than the limit")
    end = buf.find(b"\0")
    return bytes(buf if end < 0 else buf[: end + 1])


async def _connect(
    host: str, port: int, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.open_connection(host, port)
    except (OSError, TimeoutError) as exc:
        raise ClamdUnavailable(f"clamd not reachable: {type(exc).__name__}") from exc


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(1.0):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass


async def _simple_command(host: str, port: int, command: bytes, timeout: float) -> str | None:
    """One command, one reply; ``None`` on any failure."""
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(command)
                await writer.drain()
                raw = await _read_reply(reader)
            finally:
                await _close(writer)
    except (OSError, TimeoutError, ClamdError):
        return None
    if not raw.endswith(b"\0"):
        return None
    return raw[:-1].decode("utf-8", errors="replace")


async def clamd_ping(host: str, port: int, *, timeout: float) -> bool:
    """True when clamd answers ``PING`` with exactly ``PONG``."""
    return await _simple_command(host, port, b"zPING\0", timeout) == "PONG"


async def clamd_version(host: str, port: int, *, timeout: float) -> str | None:
    """clamd's ``VERSION`` line (engine / signature version and date), sanitized."""
    reply = await _simple_command(host, port, b"zVERSION\0", timeout)
    return sanitize_signature(reply) if reply else None


async def clamd_instream(
    host: str,
    port: int,
    chunks: AsyncIterable[bytes],
    *,
    expected_bytes: int,
    connect_timeout: float,
    scan_timeout: float,
) -> ClamdVerdict:
    """Stream ``chunks`` (exactly ``expected_bytes`` bytes) to clamd and classify the reply.

    The whole call, connect included, is bounded by ``scan_timeout``; the
    connect alone by ``connect_timeout``. Nothing is buffered beyond one chunk.

    Raises:
        ClamdUnavailable: nothing was streamed (clamd down).
        ClamdSizeLimit: clamd refused the stream size.
        ClamdScanFailed: the scan started but gave no usable verdict.
    """
    streamed = False
    try:
        async with asyncio.timeout(scan_timeout):
            reader, writer = await _connect(host, port, connect_timeout)
            try:
                try:
                    writer.write(b"zINSTREAM\0")
                    await writer.drain()
                except OSError as exc:
                    raise ClamdUnavailable(f"clamd write failed: {type(exc).__name__}") from exc
                sent = 0
                try:
                    async for chunk in chunks:
                        for start in range(0, len(chunk), CHUNK_BYTES):
                            piece = chunk[start : start + CHUNK_BYTES]
                            sent += len(piece)
                            if sent > expected_bytes:
                                raise ClamdScanFailed(
                                    f"object is longer than the expected {expected_bytes} bytes"
                                )
                            streamed = True
                            writer.write(struct.pack("!I", len(piece)) + piece)
                            await writer.drain()
                    if sent != expected_bytes:
                        raise ClamdScanFailed(
                            f"object stream ended after {sent} of {expected_bytes} bytes"
                        )
                    streamed = True
                    writer.write(_TERMINATOR)
                    await writer.drain()
                except OSError as exc:
                    if not isinstance(exc, ConnectionError):
                        raise  # the chunk source's own error (storage read)
                    # clamd closed mid-stream: a size-limit refusal is replied
                    # before the close; read what is buffered before deciding.
                    reply = await _buffered_reply(reader)
                    if reply:
                        parse_instream_reply(reply)
                    raise ClamdScanFailed(f"clamd closed the stream: {type(exc).__name__}") from exc
                try:
                    raw = await _read_reply(reader)
                except ConnectionError as exc:
                    raise ClamdScanFailed(f"clamd reset: {type(exc).__name__}") from exc
                return parse_instream_reply(raw)
            finally:
                await _close(writer)
    except TimeoutError as exc:
        if not streamed:
            raise ClamdUnavailable("clamd timed out before streaming") from exc
        raise ClamdScanFailed("clamd scan timed out") from exc


async def _buffered_reply(reader: asyncio.StreamReader) -> bytes:
    try:
        async with asyncio.timeout(1.0):
            return await _read_reply(reader)
    except (OSError, TimeoutError, ClamdError):
        return b""
