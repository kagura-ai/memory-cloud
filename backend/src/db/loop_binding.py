"""Bind a process-global async client to the event loop that uses it (#1995, #2241).

An async client's sockets and pool waiters belong to the loop that first used
them. ``db.redis`` and ``db.qdrant`` keep one module-global client; when the
running loop is not the one a client was built for, the caller drops the old
client (its loop is usually gone, so it cannot be closed from there) and builds
a new one. A uvicorn worker runs one loop, so production builds once; tests run
a pytest-asyncio session loop plus one loop per ``TestClient`` portal.

A client is tagged with a weak reference to its loop, or ``None`` while it was
built outside a running loop and no loop has used it yet. Only clients the
owning module tagged are ever rebuilt: a fake that a test installs has no tag
and is returned unchanged.
"""

import asyncio
import weakref

# Name of the attribute that records which loop a client belongs to.
LOOP_ATTR = "_kagura_loop"
_UNTAGGED = object()


def running_loop() -> asyncio.AbstractEventLoop | None:
    """The running event loop, or ``None`` outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def tag_loop(client: object, loop: asyncio.AbstractEventLoop | None) -> None:
    """Record ``loop`` as the owner of ``client`` (``None``: not claimed yet)."""
    try:
        setattr(client, LOOP_ATTR, weakref.ref(loop) if loop is not None else None)
    except (AttributeError, TypeError):
        pass  # a stand-in without attribute support stays untagged


def _loop_tag(client: object) -> object:
    """The client's loop tag, or ``_UNTAGGED`` for a client nobody tagged."""
    # Read the instance dict, not getattr(): a MagicMock invents attributes.
    return (getattr(client, "__dict__", None) or {}).get(LOOP_ATTR, _UNTAGGED)


def owned_by_another_loop(client: object, loop: asyncio.AbstractEventLoop) -> bool:
    """Whether ``client`` was tagged for a loop other than ``loop``.

    An untagged client (a test fake) and a client not yet claimed by any loop
    are never owned by another loop.
    """
    tag = _loop_tag(client)
    if not isinstance(tag, weakref.ref):
        return False  # untagged (a test fake) or not claimed by any loop yet
    return tag() is not loop


def keep_for_loop(client: object, loop: asyncio.AbstractEventLoop | None) -> bool:
    """Whether the cached ``client`` may serve ``loop``; claims an unclaimed client.

    ``False`` means the client belongs to another loop and the caller must
    drop it and build a new one. Outside a running loop nothing changes.
    """
    if loop is None:
        return True
    if _loop_tag(client) is None:
        # Built outside a running loop: the first loop that uses it owns it.
        tag_loop(client, loop)
        return True
    return not owned_by_another_loop(client, loop)
