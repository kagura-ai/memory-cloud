"""A fake clamd speaking the INSTREAM protocol on 127.0.0.1 (#565).

No real clamd is started in tests (it needs gigabytes of memory). The fake
implements what the client uses: ``zPING``, ``zVERSION`` and ``zINSTREAM``
with 4-byte big-endian length-prefixed chunks ended by a zero-length chunk,
NUL-terminated replies, and the ``StreamMaxLength`` refusal (reply, then
close the connection mid-stream).

The EICAR test string is built at runtime from two halves so this file does
not itself trip an anti-virus scanner on a developer's machine.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

_EICAR_HEAD = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$"
_EICAR_TAIL = "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR = (_EICAR_HEAD + _EICAR_TAIL).encode()
EICAR_SIGNATURE = "Eicar-Test-Signature"


@dataclass
class FakeClamd:
    """Behaviour knobs; every connection reads them when it runs."""

    stream_max_bytes: int = 25 * 1024 * 1024
    #: Raw reply (NUL included) sent instead of the computed verdict.
    reply: bytes | None = None
    #: Seconds to wait before replying to INSTREAM / PING.
    delay: float = 0.0
    #: Close the connection (no reply) after this many payload bytes.
    reset_after_bytes: int | None = None
    #: Reply to PING with something other than PONG.
    ping_reply: bytes = b"PONG\0"
    version_reply: bytes = b"ClamAV 1.4.3/27800/Thu Nov 12 09:00:00 2026\0"
    #: Payload bytes received per INSTREAM connection.
    streams: list[bytes] = field(default_factory=list)
    #: Whether each INSTREAM connection saw the zero-length terminator.
    terminated: list[bool] = field(default_factory=list)
    commands: list[bytes] = field(default_factory=list)
    connections: int = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            command = await reader.readuntil(b"\0")
            self.commands.append(command)
            if command == b"zPING\0":
                if self.delay:
                    await asyncio.sleep(self.delay)
                writer.write(self.ping_reply)
            elif command == b"zVERSION\0":
                writer.write(self.version_reply)
            elif command == b"zINSTREAM\0":
                await self._instream(reader, writer)
            else:
                writer.write(b"UNKNOWN COMMAND\0")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _instream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received = bytearray()
        self.streams.append(b"")
        self.terminated.append(False)
        index = len(self.streams) - 1
        while True:
            (size,) = struct.unpack("!I", await reader.readexactly(4))
            if size == 0:
                self.terminated[index] = True
                break
            received += await reader.readexactly(size)
            self.streams[index] = bytes(received)
            if self.reset_after_bytes is not None and len(received) >= self.reset_after_bytes:
                writer.transport.abort()
                return
            if len(received) > self.stream_max_bytes:
                writer.write(b"INSTREAM size limit exceeded. ERROR\0")
                await writer.drain()
                return
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.reply is not None:
            writer.write(self.reply)
        elif EICAR in received:
            writer.write(f"stream: {EICAR_SIGNATURE} FOUND\0".encode())
        else:
            writer.write(b"stream: OK\0")


@asynccontextmanager
async def running_fake_clamd(fake: FakeClamd | None = None) -> AsyncIterator[tuple[FakeClamd, int]]:
    """Start ``fake`` on an ephemeral port; yields ``(fake, port)``."""
    fake = fake or FakeClamd()
    server = await asyncio.start_server(fake._handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield fake, port
    finally:
        server.close()
        await server.wait_closed()


async def free_port() -> int:
    """A port nothing listens on (bound, then released)."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port
