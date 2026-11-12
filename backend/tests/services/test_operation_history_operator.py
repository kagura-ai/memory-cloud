"""A service operator never enters the customer-visible operation history (#2767).

The writer stores ``principal_kind = 'operator'`` with no actor id (as the
``system`` surface does), so neither the item nor the ``actor`` filter can
reveal a staff address to workspace members.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.correlation import set_correlation
from auth.history_principal import set_history_principal
from services.operation_history_writer import record_operation


@pytest.fixture(autouse=True)
def attribution_scope():
    set_history_principal()
    set_correlation(None)
    yield
    set_history_principal()
    set_correlation(None)


def _write(actor: str | None):
    return record_operation(
        AsyncSession(), workspace_id=uuid4(), actor_user_id=actor, operation="context.restored"
    )


def test_operator_principal_is_stored_without_an_actor():
    set_history_principal("operator")
    row = _write("operator:ops@example.com")
    assert row is not None
    assert row.actor_user_id is None
    assert row.principal_kind == "operator"
    assert row.surface == "api"


def test_an_operator_id_never_lands_in_the_actor_column_even_without_the_principal():
    set_history_principal("session")
    row = _write("operator:ops@example.com")
    assert row is not None
    assert row.actor_user_id is None
    assert row.principal_kind == "operator"


def test_a_session_admin_keeps_its_actor():
    set_history_principal("session")
    row = _write("1001")
    assert row is not None
    assert row.actor_user_id == "1001"
    assert row.principal_kind == "session"
