"""``Workspace.has_active_billing_contract`` (Issue #1940).

A workspace is under a billing contract while the external billing service
owns its entitlement and still has it on a paid tier. Billing keeps the paid
tier through a scheduled cancellation and through the payment-failure grace
period, and pushes ``free`` only when the contract has ended, so these two
columns cover the whole window in which deletion must be refused.
"""

from __future__ import annotations

import pytest

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Workspace,
)


@pytest.mark.parametrize(
    ("plan_name", "entitlement_source", "expected"),
    [
        ("basic", ENTITLEMENT_SOURCE_EXTERNAL_BILLING, True),
        ("pro", ENTITLEMENT_SOURCE_EXTERNAL_BILLING, True),
        ("promax", ENTITLEMENT_SOURCE_EXTERNAL_BILLING, True),
        # Contract ended: billing pushed free.
        ("free", ENTITLEMENT_SOURCE_EXTERNAL_BILLING, False),
        # Locally owned (admin/comp grant): no subscription behind it.
        ("pro", ENTITLEMENT_SOURCE_ADMIN_GRANT, False),
        ("free", ENTITLEMENT_SOURCE_ADMIN_GRANT, False),
    ],
)
def test_has_active_billing_contract(plan_name, entitlement_source, expected):
    ws = Workspace(name="w", plan_name=plan_name, entitlement_source=entitlement_source)
    assert ws.has_active_billing_contract is expected
