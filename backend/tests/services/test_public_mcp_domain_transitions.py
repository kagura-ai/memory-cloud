"""The pure decision of one domain check, and the caller-side time bound (#2809).

``decide_check(snapshot, proof, routing, right_active, reserved, db_now)``
applies every rule of the contract (``docs/public-mcp-domains.md``) with no
clock and no I/O: ``db_now`` is passed positionally, as the writer passes the
database clock. ``lookup`` turns a timeout or any exception of the resolver
into an indeterminate result.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from models.public_mcp_domain import (
    PROOF_GRACE_HOURS,
    PROOF_MIN_MISSING_CHECKS,
    SUSPENDED_HOLD_DAYS,
)
from services import public_mcp_domain_dns as domain_dns
from services import public_mcp_domains as service
from services.public_mcp_domain_dns import DomainCheck
from services.public_mcp_domains import Decision, DomainSnapshot, decide_check

NOW = datetime(2026, 11, 15, 12, 0, 0)
HOUR = timedelta(hours=1)


def active(**kw) -> DomainSnapshot:
    values = {
        "state": "active",
        "suspended_reason": None,
        "proof_ok_at": NOW - HOUR,
        "proof_missing_checks": 0,
        "verified_at": NOW - timedelta(days=10),
        "suspended_at": None,
    }
    values.update(kw)
    return DomainSnapshot(**values)


def suspended(reason: str = "proof_lost", **kw) -> DomainSnapshot:
    values = {
        "state": "suspended",
        "suspended_reason": reason,
        "proof_ok_at": NOW - timedelta(days=5),
        "proof_missing_checks": 3,
        "verified_at": NOW - timedelta(days=10),
        "suspended_at": NOW - timedelta(days=1),
    }
    values.update(kw)
    return DomainSnapshot(**values)


# ----------------------------------------------------------------------------
# Rule 1: every completed check is recorded
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("proof", ["ok", "missing", "unavailable"])
@pytest.mark.parametrize("routing", ["ok", "mismatch", "unresolved"])
@pytest.mark.parametrize("snapshot", [active(), suspended()])
def test_every_check_records_time_proof_and_routing(snapshot, proof, routing) -> None:
    d = decide_check(snapshot, proof, routing, True, False, NOW)
    assert d.last_checked_at == NOW
    assert d.last_proof_result == proof
    assert d.routing_status == routing


# ----------------------------------------------------------------------------
# Rule 2: the proof counters
# ----------------------------------------------------------------------------


def test_ok_resets_the_absences_and_moves_proof_ok_at() -> None:
    d = decide_check(active(proof_missing_checks=2), "ok", "ok", True, False, NOW)
    assert d.proof_ok_at == NOW
    assert d.proof_missing_checks == 0


def test_missing_counts_one_absence_and_keeps_proof_ok_at() -> None:
    snap = active(proof_missing_checks=1)
    d = decide_check(snap, "missing", "ok", True, False, NOW)
    assert d.proof_missing_checks == 2
    assert d.proof_ok_at == snap.proof_ok_at


@pytest.mark.parametrize("snapshot", [active(proof_missing_checks=2), suspended()])
def test_unavailable_changes_neither_counter_nor_proof_ok_at(snapshot) -> None:
    d = decide_check(snapshot, "unavailable", "unresolved", True, False, NOW)
    assert d.proof_missing_checks == snapshot.proof_missing_checks
    assert d.proof_ok_at == snapshot.proof_ok_at
    assert d.state == snapshot.state
    assert d.suspended_reason == snapshot.suspended_reason


# ----------------------------------------------------------------------------
# Rule 3: an active row
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("proof", ["ok", "missing", "unavailable"])
def test_active_stays_active_and_renews_the_lease(proof) -> None:
    d = decide_check(active(), proof, "mismatch", True, False, NOW)
    assert d.state == "active"
    assert d.suspended_reason is None
    assert d.renew_lease is True
    assert d.audit_action is None


@pytest.mark.parametrize("proof", ["ok", "missing", "unavailable"])
@pytest.mark.parametrize("right_active", [True, False])
def test_reserved_suspends_an_active_row_first(proof, right_active) -> None:
    d = decide_check(active(proof_missing_checks=5), proof, "ok", right_active, True, NOW)
    assert (d.state, d.suspended_reason) == ("suspended", "reserved")
    assert d.suspended_at == NOW
    assert d.renew_lease is False
    assert d.audit_action == "suspended"


@pytest.mark.parametrize("proof", ["ok", "missing", "unavailable"])
def test_a_right_that_ended_suspends_whatever_the_lookup_said(proof) -> None:
    snap = active(proof_missing_checks=5, proof_ok_at=NOW - timedelta(days=9))
    d = decide_check(snap, proof, "ok", False, False, NOW)
    assert (d.state, d.suspended_reason) == ("suspended", "right_ended")
    assert d.suspended_at == NOW
    assert d.renew_lease is False
    assert d.audit_action == "suspended"


def test_missing_inside_the_grace_keeps_serving() -> None:
    # Three absences, but the proof was seen less than 72 hours ago.
    snap = active(proof_missing_checks=2, proof_ok_at=NOW - timedelta(hours=PROOF_GRACE_HOURS - 1))
    d = decide_check(snap, "missing", "ok", True, False, NOW)
    assert d.state == "active"
    assert d.proof_missing_checks == 3
    assert d.renew_lease is True


def test_missing_past_the_grace_but_under_three_checks_keeps_serving() -> None:
    snap = active(proof_missing_checks=0, proof_ok_at=NOW - timedelta(days=5))
    d = decide_check(snap, "missing", "ok", True, False, NOW)
    assert d.state == "active"
    assert d.proof_missing_checks == 1
    assert d.renew_lease is True


def test_proof_lost_at_the_third_absence_counted_from_the_last_proof() -> None:
    """Last ``ok`` five days ago, then absences one hour apart: suspended at the third."""
    snap = active(proof_ok_at=NOW - timedelta(days=5), proof_missing_checks=0)
    states = []
    at = NOW
    for _ in range(PROOF_MIN_MISSING_CHECKS):
        d = decide_check(snap, "missing", "ok", True, False, at)
        states.append(d.state)
        snap = active(
            proof_ok_at=d.proof_ok_at,
            proof_missing_checks=d.proof_missing_checks,
        )
        at += HOUR
    assert states == ["active", "active", "suspended"]
    assert d.suspended_reason == "proof_lost"
    assert d.audit_action == "suspended"
    assert d.renew_lease is False


def test_grace_counts_from_verified_at_when_proof_ok_at_is_missing() -> None:
    snap = active(
        proof_ok_at=None,
        verified_at=NOW - timedelta(hours=PROOF_GRACE_HOURS),
        proof_missing_checks=2,
    )
    d = decide_check(snap, "missing", "ok", True, False, NOW)
    assert (d.state, d.suspended_reason) == ("suspended", "proof_lost")


def test_grace_boundary_is_inclusive() -> None:
    snap = active(proof_ok_at=NOW - timedelta(hours=PROOF_GRACE_HOURS), proof_missing_checks=2)
    assert decide_check(snap, "missing", "ok", True, False, NOW).state == "suspended"
    snap = active(
        proof_ok_at=NOW - timedelta(hours=PROOF_GRACE_HOURS) + timedelta(seconds=1),
        proof_missing_checks=2,
    )
    assert decide_check(snap, "missing", "ok", True, False, NOW).state == "active"


def test_unavailable_between_absences_does_not_advance_the_loss() -> None:
    snap = active(proof_ok_at=NOW - timedelta(days=5), proof_missing_checks=2)
    d = decide_check(snap, "unavailable", "unresolved", True, False, NOW)
    assert d.state == "active"
    assert d.proof_missing_checks == 2
    assert d.proof_ok_at == snap.proof_ok_at
    assert d.renew_lease is True


def test_routing_lost_alone_changes_no_state() -> None:
    for routing in ("mismatch", "unresolved"):
        d = decide_check(active(), "ok", routing, True, False, NOW)
        assert d.state == "active"
        assert d.audit_action is None


def test_reason_order_on_an_active_row() -> None:
    lost = active(proof_ok_at=NOW - timedelta(days=5), proof_missing_checks=2)
    assert decide_check(lost, "missing", "ok", False, True, NOW).suspended_reason == "reserved"
    assert decide_check(lost, "missing", "ok", False, False, NOW).suspended_reason == "right_ended"
    assert decide_check(lost, "missing", "ok", True, False, NOW).suspended_reason == "proof_lost"


# ----------------------------------------------------------------------------
# Rule 4: a suspended row
# ----------------------------------------------------------------------------


def test_suspended_with_proof_and_right_is_reinstated() -> None:
    snap = suspended("right_ended")
    d = decide_check(snap, "ok", "ok", True, False, NOW)
    assert (d.state, d.suspended_reason, d.suspended_at) == ("active", None, None)
    assert d.renew_lease is True
    assert d.audit_action == "reinstated"
    assert d.proof_ok_at == NOW
    assert d.proof_missing_checks == 0


def test_reason_order_on_a_suspended_row() -> None:
    snap = suspended("proof_lost")
    assert decide_check(snap, "ok", "ok", False, True, NOW).suspended_reason == "reserved"
    assert decide_check(snap, "ok", "ok", False, False, NOW).suspended_reason == "right_ended"
    assert decide_check(snap, "ok", "ok", True, True, NOW).suspended_reason == "reserved"
    assert decide_check(snap, "missing", "ok", True, False, NOW).suspended_reason == "proof_lost"


@pytest.mark.parametrize(
    ("before", "proof", "right_active", "reserved", "after"),
    [
        ("reserved", "missing", True, False, "proof_lost"),
        ("right_ended", "missing", True, False, "proof_lost"),
        ("proof_lost", "ok", False, False, "right_ended"),
        ("proof_lost", "missing", True, True, "reserved"),
        ("reserved", "unavailable", True, False, "reserved"),
    ],
)
def test_a_change_of_reason_keeps_suspended_at_and_writes_no_audit(
    before, proof, right_active, reserved, after
) -> None:
    snap = suspended(before)
    d = decide_check(snap, proof, "ok", right_active, reserved, NOW)
    assert (d.state, d.suspended_reason) == ("suspended", after)
    assert d.suspended_at == snap.suspended_at
    assert d.audit_action is None
    assert d.renew_lease is False


@pytest.mark.parametrize(
    ("proof", "right_active", "reserved"),
    [
        ("missing", True, False),
        ("unavailable", True, False),
        ("ok", False, False),
        ("ok", True, True),
    ],
)
def test_thirty_days_suspended_is_released(proof, right_active, reserved) -> None:
    snap = suspended(suspended_at=NOW - timedelta(days=SUSPENDED_HOLD_DAYS))
    d = decide_check(snap, proof, "ok", right_active, reserved, NOW)
    assert d.state == "released"
    assert d.suspended_reason is None
    assert d.audit_action == "released"
    assert d.renew_lease is False


def test_reinstated_at_the_thirtieth_day_is_not_released() -> None:
    snap = suspended(suspended_at=NOW - timedelta(days=SUSPENDED_HOLD_DAYS + 3))
    d = decide_check(snap, "ok", "ok", True, False, NOW)
    assert d.state == "active"
    assert d.audit_action == "reinstated"


def test_under_thirty_days_the_hold_stays() -> None:
    snap = suspended(suspended_at=NOW - timedelta(days=SUSPENDED_HOLD_DAYS) + HOUR)
    d = decide_check(snap, "missing", "ok", True, False, NOW)
    assert d.state == "suspended"


def test_decision_is_a_value() -> None:
    d = decide_check(active(), "ok", "ok", True, False, NOW)
    assert isinstance(d, Decision)
    assert d == decide_check(active(), "ok", "ok", True, False, NOW)


def test_db_now_is_positional() -> None:
    import inspect

    params = list(inspect.signature(decide_check).parameters.values())
    assert [p.name for p in params] == [
        "snapshot",
        "proof",
        "routing",
        "right_active",
        "reserved",
        "db_now",
    ]
    assert params[-1].kind is inspect.Parameter.POSITIONAL_ONLY


# ----------------------------------------------------------------------------
# lookup: the caller-side time bound
# ----------------------------------------------------------------------------


class _Resolver:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[str, str, str]] = []

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        self.calls.append((hostname, token, routing_target))
        return await self.behaviour()


async def test_lookup_returns_the_resolver_answer(monkeypatch) -> None:
    from config.settings import get_settings

    async def ok() -> DomainCheck:
        return DomainCheck("ok", "mismatch")

    fake = _Resolver(ok)
    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: fake)
    monkeypatch.setattr(get_settings(), "mcp_public_domains_routing_target", "edge.example.net")
    assert await service.lookup("alias.example.org", "t" * 64) == DomainCheck("ok", "mismatch")
    assert fake.calls == [("alias.example.org", "t" * 64, "edge.example.net")]


async def test_lookup_turns_a_timeout_into_unavailable(monkeypatch) -> None:
    async def slow() -> DomainCheck:
        await asyncio.sleep(5)
        return DomainCheck("missing", "ok")

    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: _Resolver(slow))
    monkeypatch.setattr(service, "CHECK_TIMEOUT_SECONDS", 0.01)
    assert await service.lookup("alias.example.org", "t") == DomainCheck(
        "unavailable", "unresolved"
    )


async def test_lookup_turns_an_exception_into_unavailable(monkeypatch) -> None:
    async def broken() -> DomainCheck:
        raise OSError("resolver gone")

    monkeypatch.setattr(domain_dns, "domain_resolver", lambda: _Resolver(broken))
    assert await service.lookup("alias.example.org", "t") == DomainCheck(
        "unavailable", "unresolved"
    )


async def test_lookup_turns_a_failing_factory_into_unavailable(monkeypatch) -> None:
    def factory():
        raise RuntimeError("no resolver configuration")

    monkeypatch.setattr(domain_dns, "domain_resolver", factory)
    assert await service.lookup("alias.example.org", "t") == DomainCheck(
        "unavailable", "unresolved"
    )
