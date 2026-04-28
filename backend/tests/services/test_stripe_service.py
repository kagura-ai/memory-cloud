"""Tests for Stripe service async wrappers (Issue #468).

Covers the two ``_run_stripe*`` helpers introduced to keep synchronous
``stripe-python`` calls off the asyncio event loop, and the dedicated
ThreadPoolExecutor used by the GDPR erasure sweep.
"""

import asyncio
import threading
import time

import pytest

import services.stripe_service as stripe_service
from services.stripe_service import (
    _run_stripe,
    _run_stripe_erasure,
    shutdown_erasure_executor,
)


@pytest.fixture(autouse=True)
def reset_erasure_executor():
    """Each test starts and ends with no live erasure executor."""
    shutdown_erasure_executor()
    yield
    shutdown_erasure_executor()


@pytest.mark.asyncio
async def test_run_stripe_does_not_block_event_loop():
    """A 100ms sync call must not block a 50ms concurrent asyncio.sleep.

    Flake budget: the asyncio.sleep target is 50ms; we accept up to 90ms
    real wall-clock for it to complete, which leaves 40ms of CI jitter
    headroom while still catching a regression that would push it past
    the 100ms ``time.sleep`` boundary.
    """

    def slow_sync_call() -> str:
        time.sleep(0.1)
        return "ok"

    asyncio_done_at: list[float] = []

    async def measure_asyncio_sleep() -> None:
        await asyncio.sleep(0.05)
        asyncio_done_at.append(time.monotonic())

    start = time.monotonic()
    sleep_task = asyncio.create_task(measure_asyncio_sleep())
    result = await _run_stripe(slow_sync_call)
    stripe_done_at = time.monotonic()
    await sleep_task

    assert result == "ok"
    assert asyncio_done_at, "concurrent asyncio.sleep never ran"
    asyncio_elapsed = asyncio_done_at[0] - start
    assert asyncio_elapsed < 0.09, (
        f"asyncio.sleep appears blocked: completed at {asyncio_elapsed:.3f}s, "
        "expected < 0.09s — the event loop did not yield during the wrapped "
        "synchronous call"
    )
    assert stripe_done_at - start >= 0.1


@pytest.mark.asyncio
async def test_run_stripe_preserves_args_and_kwargs():
    """The wrapper must forward both positional and keyword args verbatim."""
    captured: dict = {}

    def fake_create(*args, **kwargs) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "session-id"

    result = await _run_stripe(
        fake_create,
        "pos1",
        "pos2",
        idempotency_key="ik-123",
        expand=["customer"],
    )

    assert result == "session-id"
    assert captured["args"] == ("pos1", "pos2")
    assert captured["kwargs"] == {"idempotency_key": "ik-123", "expand": ["customer"]}


@pytest.mark.asyncio
async def test_run_stripe_erasure_preserves_kwargs_via_partial():
    """``_run_stripe_erasure`` uses ``functools.partial`` so kwargs survive
    the ``run_in_executor`` boundary (which only forwards positional args)."""
    captured: dict = {}

    def fake_cancel(sub_id: str, *, idempotency_key: str | None = None) -> dict:
        captured["sub_id"] = sub_id
        captured["idempotency_key"] = idempotency_key
        return {"cancelled": True}

    result = await _run_stripe_erasure(fake_cancel, "sub_123", idempotency_key="erasure-1")

    assert result == {"cancelled": True}
    assert captured == {"sub_id": "sub_123", "idempotency_key": "erasure-1"}


@pytest.mark.asyncio
async def test_run_stripe_erasure_uses_dedicated_executor():
    """The dedicated executor's threads carry the ``stripe-erasure`` prefix."""
    thread_names: list[str] = []

    def capture_thread_name() -> None:
        thread_names.append(threading.current_thread().name)

    await _run_stripe_erasure(capture_thread_name)

    assert thread_names, "wrapped call did not run"
    assert thread_names[0].startswith("stripe-erasure"), (
        f"expected dedicated erasure thread, got {thread_names[0]!r}"
    )


@pytest.mark.asyncio
async def test_run_stripe_uses_default_pool_not_erasure_pool():
    """``_run_stripe`` must NOT borrow threads from the dedicated erasure pool."""
    thread_names: list[str] = []

    def capture_thread_name() -> None:
        thread_names.append(threading.current_thread().name)

    await _run_stripe(capture_thread_name)

    assert thread_names, "wrapped call did not run"
    assert not thread_names[0].startswith("stripe-erasure"), (
        f"_run_stripe should use the default pool, got {thread_names[0]!r}"
    )


def test_shutdown_erasure_executor_is_noop_when_uninitialized():
    """Calling shutdown before any wrapped erasure call must be safe."""
    assert stripe_service._erasure_executor is None
    shutdown_erasure_executor()
    shutdown_erasure_executor()
    assert stripe_service._erasure_executor is None


@pytest.mark.asyncio
async def test_shutdown_erasure_executor_after_use_is_idempotent():
    """After a real call the executor exists; shutdown clears it and the
    second shutdown is a no-op (mirrors the lifespan re-entry path)."""

    def noop() -> None:
        return None

    await _run_stripe_erasure(noop)
    assert stripe_service._erasure_executor is not None

    shutdown_erasure_executor()
    assert stripe_service._erasure_executor is None

    shutdown_erasure_executor()
    assert stripe_service._erasure_executor is None
