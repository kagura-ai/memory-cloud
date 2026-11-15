"""The custom domain maintenance job and its run, without a database (#2809).

- the job is registered only while ``MCP_PUBLIC_DOMAINS_ENABLED`` is on;
- the job returns integer counts under fixed names, ``job_failed`` when the
  run raised, and does nothing while the switch is off;
- a run checks the due rows at most ``MAINTENANCE_CONCURRENCY`` at a time,
  applies every completed lookup (``unavailable`` included) through
  ``apply_check`` and counts what was done; past the run budget a due row is
  counted ``budget_skipped`` and is not looked up.

The database behaviour (housekeeping, the due order, the fence, the pool) is
in ``tests/integration/test_public_mcp_domain_maintenance_db.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import get_settings
from models.public_mcp_domain import MAINTENANCE_CONCURRENCY, MAINTENANCE_INTERVAL_SECONDS
from services import public_mcp_domain_maintenance as maintenance
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from services.public_mcp_domains import SYSTEM_ACTOR, AppliedCheck
from tasks import public_mcp_domain_tasks as job
from tasks.job_runs import job_failed

COUNT_NAMES = (
    "checked",
    "proof_ok",
    "proof_missing",
    "dns_unavailable",
    "suspended",
    "reinstated",
    "released",
    "pending_expired",
    "orphans_released",
    "purged",
    "fenced",
    "budget_skipped",
)


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()

    def set_switch(on: bool) -> None:
        monkeypatch.setattr(settings, "mcp_public_domains_routing_target", "edge.example.net")
        monkeypatch.setattr(settings, "mcp_public_domains_enabled", on)

    return set_switch


# ----------------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------------


def test_registered_when_the_switch_is_on(switch) -> None:
    switch(True)
    scheduler = MagicMock()
    job.schedule_public_mcp_domain_tasks(scheduler)
    scheduler.add_job.assert_called_once()
    args, kwargs = scheduler.add_job.call_args
    assert args[0] is job.public_mcp_domain_maintenance_task
    assert kwargs["id"] == job.JOB_ID == "public_mcp_domain_maintenance"
    assert isinstance(kwargs["trigger"], IntervalTrigger)
    assert kwargs["trigger"].interval.total_seconds() == MAINTENANCE_INTERVAL_SECONDS == 900
    assert kwargs["replace_existing"] is True
    # The first run starts with the scheduler, not one interval later.
    assert kwargs["next_run_time"] <= datetime.now(UTC)


def test_not_registered_when_the_switch_is_off(switch) -> None:
    switch(False)
    scheduler = MagicMock()
    job.schedule_public_mcp_domain_tasks(scheduler)
    scheduler.add_job.assert_not_called()


@pytest.mark.parametrize("on", [True, False])
def test_the_leader_registers_it_only_when_on(switch, on: bool) -> None:
    import tasks

    switch(on)
    scheduler = AsyncIOScheduler(timezone="UTC")
    tasks.register_scheduled_jobs(scheduler)
    ids = {j.id for j in scheduler.get_jobs()}
    assert (job.JOB_ID in ids) is on
    assert "schedule_public_mcp_domain_tasks" in tasks.__all__


# ----------------------------------------------------------------------------
# The job entry point
# ----------------------------------------------------------------------------


async def test_the_job_returns_integer_counts(switch, monkeypatch: pytest.MonkeyPatch) -> None:
    switch(True)
    counts = maintenance.MaintenanceCounts(checked=3, proof_ok=2, dns_unavailable=1, purged=4)
    run = AsyncMock(return_value=counts)
    monkeypatch.setattr(job, "run_maintenance", run)
    result = await job.public_mcp_domain_maintenance_task()
    run.assert_awaited_once_with()
    assert isinstance(result, dict)
    assert tuple(result) == COUNT_NAMES
    assert all(type(value) is int for value in result.values())
    assert (result["checked"], result["proof_ok"], result["purged"]) == (3, 2, 4)


async def test_an_exception_is_job_failed(switch, monkeypatch: pytest.MonkeyPatch) -> None:
    switch(True)
    monkeypatch.setattr(job, "run_maintenance", AsyncMock(side_effect=ConnectionError("db")))
    log = MagicMock()
    monkeypatch.setattr(job, "logger", log)
    assert await job.public_mcp_domain_maintenance_task() == job_failed(ConnectionError())
    log.warning.assert_called_once_with(
        "public_mcp_domain_maintenance_failed", error_class="ConnectionError"
    )


async def test_the_job_does_nothing_while_the_switch_is_off(
    switch, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch(False)
    run = AsyncMock()
    monkeypatch.setattr(job, "run_maintenance", run)
    assert await job.public_mcp_domain_maintenance_task() == {"skipped": 1}
    run.assert_not_awaited()


def test_the_task_module_names_no_entitlement_symbol() -> None:
    from pathlib import Path

    source = Path(job.__file__).read_text(encoding="utf-8")
    for name in ("integration_entitlements", "paid_integration", "inspect_paid"):
        assert name not in source


# ----------------------------------------------------------------------------
# One run (database calls replaced)
# ----------------------------------------------------------------------------


class _Sessions:
    """A session factory whose sessions are mocks; counts the opened ones."""

    def __init__(self) -> None:
        self.opened = 0
        self.open_now = 0
        self.max_open = 0

    def __call__(self) -> Any:
        @asynccontextmanager
        async def session() -> AsyncIterator[MagicMock]:
            self.opened += 1
            self.open_now += 1
            self.max_open = max(self.max_open, self.open_now)
            db = MagicMock()
            db.commit = AsyncMock()
            db.rollback = AsyncMock()
            try:
                yield db
            finally:
                self.open_now -= 1

        return session()


def _due(n: int) -> list[maintenance.DueDomain]:
    return [maintenance.DueDomain(uuid4(), i, f"d{i}.example.org", f"{i:064x}") for i in range(n)]


@pytest.fixture
def run_env(monkeypatch: pytest.MonkeyPatch):
    """Replace the database steps of a run; returns the due list to fill."""
    due: list[maintenance.DueDomain] = []
    monkeypatch.setattr(maintenance, "housekeeping", AsyncMock())
    monkeypatch.setattr(maintenance, "due_domains", AsyncMock(side_effect=lambda db: list(due)))
    return due


async def test_every_completed_lookup_is_applied_and_counted(
    run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_env.extend(_due(6))
    checks = {
        "d0.example.org": DomainCheck("ok", "ok"),
        "d1.example.org": DomainCheck("missing", "ok"),
        "d2.example.org": DomainCheck("unavailable", "unresolved"),
        "d3.example.org": DomainCheck("ok", "mismatch"),
        "d4.example.org": DomainCheck("missing", "unresolved"),
        "d5.example.org": DomainCheck("unavailable", "unresolved"),
    }
    outcomes = {
        0: AppliedCheck("applied", True, "suspended", "active", "reinstated"),
        1: AppliedCheck("applied", True, "active", "suspended", "suspended", "proof_lost"),
        2: AppliedCheck("applied", True, "active", "active"),
        3: AppliedCheck("fenced", True),
        4: AppliedCheck("applied", True, "suspended", "released", "released"),
        5: AppliedCheck("applied", False, "active", "suspended", "suspended", "right_ended"),
    }
    lookup = AsyncMock(side_effect=lambda hostname, token: checks[hostname])
    applied: list[tuple[Any, ...]] = []

    async def apply_check(db, domain_id, seen_seq, check, actor):
        applied.append((domain_id, seen_seq, check, actor))
        return outcomes[seen_seq]

    monkeypatch.setattr(service, "lookup", lookup)
    monkeypatch.setattr(service, "apply_check", apply_check)
    sessions = _Sessions()

    counts = await maintenance.run_maintenance(sessions)

    by_id = {item.id: item for item in run_env}
    assert len(applied) == 6
    for domain_id, seen_seq, check, actor in applied:
        assert seen_seq == by_id[domain_id].check_seq
        assert check == checks[by_id[domain_id].hostname]
        assert actor is SYSTEM_ACTOR
    assert counts.as_dict() == {
        "checked": 6,
        "proof_ok": 2,
        "proof_missing": 2,
        "dns_unavailable": 2,
        "suspended": 2,
        "reinstated": 1,
        "released": 1,
        "pending_expired": 0,
        "orphans_released": 0,
        "purged": 0,
        "fenced": 1,
        "budget_skipped": 0,
    }


async def test_no_session_is_open_during_a_lookup(run_env, monkeypatch: pytest.MonkeyPatch) -> None:
    run_env.extend(_due(8))
    sessions = _Sessions()
    open_during_lookup: list[int] = []

    async def lookup(hostname, token):
        open_during_lookup.append(sessions.open_now)
        await asyncio.sleep(0)
        return DomainCheck("ok", "ok")

    monkeypatch.setattr(service, "lookup", lookup)
    monkeypatch.setattr(
        service, "apply_check", AsyncMock(return_value=AppliedCheck("applied", True))
    )
    await maintenance.run_maintenance(sessions)
    # Another worker may be applying its own check: never more than the
    # number of workers minus the one looking up.
    assert len(open_during_lookup) == 8
    assert max(open_during_lookup) <= MAINTENANCE_CONCURRENCY - 1
    assert sessions.max_open <= MAINTENANCE_CONCURRENCY


async def test_lookups_run_at_most_the_concurrency_at_a_time(
    run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_env.extend(_due(12))
    running = 0
    peak = 0

    async def lookup(hostname, token):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return DomainCheck("ok", "ok")

    monkeypatch.setattr(service, "lookup", lookup)
    monkeypatch.setattr(
        service, "apply_check", AsyncMock(return_value=AppliedCheck("applied", True))
    )
    counts = await maintenance.run_maintenance(_Sessions())
    assert counts.checked == 12
    assert peak == MAINTENANCE_CONCURRENCY


async def test_past_the_budget_rows_are_budget_skipped(
    run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_env.extend(_due(5))
    lookup = AsyncMock()
    monkeypatch.setattr(service, "lookup", lookup)
    monkeypatch.setattr(service, "apply_check", AsyncMock())
    monkeypatch.setattr(maintenance, "RUN_BUDGET_SECONDS", 0)
    counts = await maintenance.run_maintenance(_Sessions())
    assert counts.budget_skipped == 5
    assert counts.checked == 0
    lookup.assert_not_awaited()


async def test_the_budget_ends_a_run_part_way(run_env, monkeypatch: pytest.MonkeyPatch) -> None:
    run_env.extend(_due(10))
    clock = [0.0]
    monkeypatch.setattr(maintenance, "_clock", lambda: clock[0])
    monkeypatch.setattr(maintenance, "RUN_BUDGET_SECONDS", 2.5)

    async def lookup(hostname, token):
        clock[0] += 1.0
        await asyncio.sleep(0)
        return DomainCheck("ok", "ok")

    monkeypatch.setattr(service, "lookup", lookup)
    monkeypatch.setattr(
        service, "apply_check", AsyncMock(return_value=AppliedCheck("applied", True))
    )
    counts = await maintenance.run_maintenance(_Sessions())
    assert 1 <= counts.checked < 10
    assert counts.budget_skipped >= 1
    assert counts.checked + counts.budget_skipped == 10


def test_the_run_budget_is_half_the_interval_at_most_two_minutes() -> None:
    assert maintenance.RUN_BUDGET_SECONDS == min(MAINTENANCE_INTERVAL_SECONDS / 2, 120)


async def test_a_failed_write_fails_the_run(run_env, monkeypatch: pytest.MonkeyPatch) -> None:
    run_env.extend(_due(3))
    monkeypatch.setattr(service, "lookup", AsyncMock(return_value=DomainCheck("ok", "ok")))
    monkeypatch.setattr(service, "apply_check", AsyncMock(side_effect=ConnectionError("db")))
    with pytest.raises(ConnectionError):
        await maintenance.run_maintenance(_Sessions())


async def test_the_job_fails_when_the_run_fails(
    switch, run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch(True)
    run_env.extend(_due(2))
    monkeypatch.setattr(service, "lookup", AsyncMock(return_value=DomainCheck("ok", "ok")))
    monkeypatch.setattr(service, "apply_check", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(maintenance, "_get_session_factory", lambda: _Sessions())
    assert await job.public_mcp_domain_maintenance_task() == job_failed(TimeoutError())
