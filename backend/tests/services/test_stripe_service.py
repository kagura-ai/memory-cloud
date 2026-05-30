"""Tests for Stripe service async wrappers (Issue #468).

Covers the two ``_run_stripe*`` helpers introduced to keep synchronous
``stripe-python`` calls off the asyncio event loop, and the dedicated
ThreadPoolExecutor used by the GDPR erasure sweep.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import services.stripe_service as stripe_service
from models.auth import Workspace
from services.stripe_service import (
    _run_stripe,
    _run_stripe_erasure,
    create_checkout_session,
    shutdown_erasure_executor,
)
from utils.exceptions import StripeError


@pytest.fixture(autouse=True)
def reset_erasure_executor():
    """Each test starts and ends with no live erasure executor."""
    shutdown_erasure_executor()
    yield
    shutdown_erasure_executor()


@pytest.mark.asyncio
async def test_run_stripe_does_not_block_event_loop():
    """The wrapped sync call must run without preventing async progress.

    Causal (not temporal) test: the sync call blocks on
    ``allow_sync_finish`` until a concurrent asyncio task sets
    ``async_progress`` and releases it. If the wrapper blocked the event
    loop, ``allow_sync_finish.wait`` would time out and the test would
    fail fast — no wall-clock thresholds, no CI flake budget.
    """

    sync_started = threading.Event()
    async_progress = threading.Event()
    allow_sync_finish = threading.Event()

    def slow_sync_call() -> str:
        sync_started.set()
        assert allow_sync_finish.wait(timeout=1), (
            "sync call was never unblocked by the concurrent asyncio task"
        )
        assert async_progress.is_set(), "concurrent asyncio task never made progress"
        return "ok"

    async def unblock_sync_call() -> None:
        await asyncio.to_thread(sync_started.wait)
        await asyncio.sleep(0)
        async_progress.set()
        allow_sync_finish.set()

    unblock_task = asyncio.create_task(unblock_sync_call())
    result = await _run_stripe(slow_sync_call)
    await unblock_task

    assert result == "ok"
    assert async_progress.is_set(), "concurrent asyncio task never ran"


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
async def test_create_checkout_session_raises_stripe_error_on_missing_url():
    """Boundary guard: stripe-python types ``Session.url`` as
    ``Optional[str]`` because non-redirect modes leave it unset. We
    always pass ``mode="subscription"`` with ``success_url``/``cancel_url``,
    so a ``None`` here means an unexpected upstream change — raise
    ``StripeError`` (typed 502) instead of returning ``None`` into the
    redirect path.
    """
    workspace_id = uuid4()
    workspace = MagicMock()
    workspace.id = workspace_id
    workspace.stripe_customer_id = None

    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = workspace
    db = AsyncMock()
    db.execute.return_value = result_proxy

    fake_session = MagicMock()
    fake_session.id = "cs_test_123"
    fake_session.url = None

    with (
        patch("services.stripe_service._init_stripe"),
        patch("services.stripe_service.get_price_id", return_value="price_test"),
        patch("stripe.checkout.Session.create", return_value=fake_session),
    ):
        with pytest.raises(StripeError, match="checkout Session.create returned no URL"):
            await create_checkout_session(
                db,
                workspace_id,
                "basic",
                "https://example.com/success",
                "https://example.com/cancel",
            )


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


# ---------------------------------------------------------------------------
# create_portal_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_portal_session_missing_customer_raises():
    workspace_id = uuid4()
    workspace = MagicMock()
    workspace.id = workspace_id
    workspace.stripe_customer_id = None

    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = workspace
    db = AsyncMock()
    db.execute.return_value = result_proxy

    with patch("services.stripe_service._init_stripe"):
        with pytest.raises(ValueError, match="No Stripe customer linked"):
            from services.stripe_service import create_portal_session

            await create_portal_session(db, workspace_id, "https://example.com/return")


@pytest.mark.asyncio
async def test_create_portal_session_success():
    workspace_id = uuid4()
    workspace = MagicMock()
    workspace.id = workspace_id
    workspace.stripe_customer_id = "cus_123"

    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = workspace
    db = AsyncMock()
    db.execute.return_value = result_proxy

    fake_session = MagicMock()
    fake_session.url = "https://billing.stripe.com/session/portal_123"

    with (
        patch("services.stripe_service._init_stripe"),
        patch(
            "services.stripe_service._run_stripe", new_callable=AsyncMock, return_value=fake_session
        ),
    ):
        from services.stripe_service import create_portal_session

        url = await create_portal_session(db, workspace_id, "https://example.com/return")
        assert url == "https://billing.stripe.com/session/portal_123"


# ---------------------------------------------------------------------------
# handle_webhook_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_webhook_checkout_completed():
    db = AsyncMock()
    settings = MagicMock()
    settings.stripe_webhook_secret = "whsec_test"

    event = {
        "id": "evt_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"workspace_id": str(uuid4()), "plan_name": "pro"},
                "customer": "cus_123",
                "subscription": "sub_123",
            }
        },
    }

    with (
        patch("services.stripe_service.get_settings", return_value=settings),
        patch("stripe.Webhook.construct_event", return_value=event),
        patch("services.stripe_service._apply_plan_change", new_callable=AsyncMock) as mock_apply,
    ):
        from services.stripe_service import handle_webhook_event

        result = await handle_webhook_event(db, b"payload", "sig")
        assert result["event_type"] == "checkout.session.completed"
        assert result["status"] == "processed"
        mock_apply.assert_called_once()


@pytest.mark.asyncio
async def test_handle_webhook_subscription_deleted():
    db = AsyncMock()
    settings = MagicMock()
    settings.stripe_webhook_secret = "whsec_test"

    event = {
        "id": "evt_456",
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_456"}},
    }

    with (
        patch("services.stripe_service.get_settings", return_value=settings),
        patch("stripe.Webhook.construct_event", return_value=event),
        patch(
            "services.stripe_service._handle_subscription_cancelled", new_callable=AsyncMock
        ) as mock_cancel,
    ):
        from services.stripe_service import handle_webhook_event

        result = await handle_webhook_event(db, b"payload", "sig")
        assert result["event_type"] == "customer.subscription.deleted"
        mock_cancel.assert_called_once_with(db, "cus_456")


@pytest.mark.asyncio
async def test_handle_webhook_payment_failed():
    db = AsyncMock()
    settings = MagicMock()
    settings.stripe_webhook_secret = "whsec_test"

    event = {
        "id": "evt_789",
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_789", "id": "in_789"}},
    }

    with (
        patch("services.stripe_service.get_settings", return_value=settings),
        patch("stripe.Webhook.construct_event", return_value=event),
    ):
        from services.stripe_service import handle_webhook_event

        result = await handle_webhook_event(db, b"payload", "sig")
        assert result["event_type"] == "invoice.payment_failed"


@pytest.mark.asyncio
async def test_handle_webhook_missing_secret_raises():
    db = AsyncMock()
    settings = MagicMock()
    settings.stripe_webhook_secret = None

    with patch("services.stripe_service.get_settings", return_value=settings):
        from services.stripe_service import handle_webhook_event

        with pytest.raises(ValueError, match="STRIPE_WEBHOOK_SECRET not configured"):
            await handle_webhook_event(db, b"payload", "sig")


# ---------------------------------------------------------------------------
# _apply_plan_change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_plan_change_workspace_not_found():
    db = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = None
    db.execute.return_value = result_proxy

    with patch("services.stripe_service.get_plan_tier") as mock_tier:
        from services.stripe_service import _apply_plan_change

        await _apply_plan_change(db, uuid4(), "pro", "cus_123", "sub_123")
        mock_tier.assert_not_called()


@pytest.mark.asyncio
async def test_apply_plan_change_success():
    # spec_set=Workspace: setting workspace.memory_limit would raise (the column
    # was dropped in #805), so this mock actively prevents a writer being
    # reintroduced — alongside the explicit daily/weekly assertions below.
    workspace = MagicMock(spec_set=Workspace)
    workspace.id = uuid4()
    workspace.plan_name = "free"

    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = workspace
    db = AsyncMock()
    db.execute.return_value = result_proxy

    tier = MagicMock()
    tier.memory_limit = 500
    tier.daily_api_limit = 1000
    tier.weekly_api_limit = 5000

    with patch("services.stripe_service.get_plan_tier", return_value=tier):
        from services.stripe_service import _apply_plan_change

        await _apply_plan_change(db, workspace.id, "pro", "cus_123", "sub_123")

    assert workspace.plan_name == "pro"
    # #805: memory_limit is no longer a Workspace column (SSoT = plan_tier) — the
    # spec_set mock makes a reintroduced `workspace.memory_limit = ...` write fail
    # loudly. The remaining quota columns must still be synced from the tier.
    assert workspace.daily_api_limit == 1000
    assert workspace.weekly_api_limit == 5000
    assert workspace.stripe_customer_id == "cus_123"
    assert workspace.stripe_subscription_id == "sub_123"
    db.add.assert_called_once()
    db.commit.assert_called()


# ---------------------------------------------------------------------------
# _handle_subscription_cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_subscription_cancelled_downgrades_to_free():
    # spec_set=Workspace: a reintroduced workspace.memory_limit write would raise
    # (column dropped in #805); daily/weekly remain synced from the free tier.
    workspace = MagicMock(spec_set=Workspace)
    workspace.id = uuid4()
    workspace.plan_name = "pro"

    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = workspace
    db = AsyncMock()
    db.execute.return_value = result_proxy

    free_tier = MagicMock()
    free_tier.memory_limit = 100
    free_tier.daily_api_limit = 100
    free_tier.weekly_api_limit = 500

    with patch("services.stripe_service.get_plan_tier", return_value=free_tier):
        from services.stripe_service import _handle_subscription_cancelled

        await _handle_subscription_cancelled(db, "cus_123")

    assert workspace.plan_name == "free"
    # #805: memory_limit column dropped — downgrade no longer syncs it (spec_set
    # guards against reintroduction); daily/weekly are still synced from the tier.
    assert workspace.daily_api_limit == 100
    assert workspace.weekly_api_limit == 500
    assert workspace.stripe_subscription_id is None
    db.add.assert_called_once()
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_handle_subscription_cancelled_workspace_not_found():
    db = AsyncMock()
    result_proxy = MagicMock()
    result_proxy.scalar_one_or_none.return_value = None
    db.execute.return_value = result_proxy

    from services.stripe_service import _handle_subscription_cancelled

    await _handle_subscription_cancelled(db, "cus_ghost")
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# cancel_subscription_and_delete_customer_for_erasure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erasure_noop_when_billing_disabled():
    workspace = MagicMock()
    workspace.stripe_customer_id = "cus_123"
    workspace.stripe_subscription_id = "sub_123"

    with patch("plugins.billing.is_billing_enabled", return_value=False):
        from services.stripe_service import cancel_subscription_and_delete_customer_for_erasure

        result = await cancel_subscription_and_delete_customer_for_erasure(workspace)
        assert result == {"subscription_cancelled": False, "customer_deleted": False}


@pytest.mark.asyncio
async def test_erasure_noop_when_no_stripe_ids():
    workspace = MagicMock()
    workspace.stripe_customer_id = None
    workspace.stripe_subscription_id = None

    with patch("plugins.billing.is_billing_enabled", return_value=True):
        from services.stripe_service import cancel_subscription_and_delete_customer_for_erasure

        result = await cancel_subscription_and_delete_customer_for_erasure(workspace)
        assert result == {"subscription_cancelled": False, "customer_deleted": False}


@pytest.mark.asyncio
async def test_erasure_success_cancel_and_delete():
    workspace = MagicMock()
    workspace.id = uuid4()
    workspace.stripe_customer_id = "cus_123"
    workspace.stripe_subscription_id = "sub_123"

    with (
        patch("plugins.billing.is_billing_enabled", return_value=True),
        patch("services.stripe_service._init_stripe"),
        patch(
            "services.stripe_service._run_stripe_erasure", new_callable=AsyncMock, return_value=None
        ),
    ):
        from services.stripe_service import cancel_subscription_and_delete_customer_for_erasure

        result = await cancel_subscription_and_delete_customer_for_erasure(workspace)
        assert result == {"subscription_cancelled": True, "customer_deleted": True}


@pytest.mark.asyncio
async def test_erasure_partial_failure_continues():
    workspace = MagicMock()
    workspace.id = uuid4()
    workspace.stripe_customer_id = "cus_123"
    workspace.stripe_subscription_id = "sub_123"

    async def fake_erasure(func, *args, **kwargs):
        if "Subscription" in str(func):
            raise RuntimeError("Stripe timeout")
        return None

    with (
        patch("plugins.billing.is_billing_enabled", return_value=True),
        patch("services.stripe_service._init_stripe"),
        patch("services.stripe_service._run_stripe_erasure", side_effect=fake_erasure),
    ):
        from services.stripe_service import cancel_subscription_and_delete_customer_for_erasure

        result = await cancel_subscription_and_delete_customer_for_erasure(workspace)
        assert result == {"subscription_cancelled": False, "customer_deleted": True}
