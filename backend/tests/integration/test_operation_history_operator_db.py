"""A service operator's history row hides the operator from workspace members (#2767).

Real PostgreSQL in a private schema (fixtures from ``test_operation_history_db``).
The writer stores no actor for an operator; a row that carries one anyway (an
older writer, a manual insert) is masked on read, and the ``actor`` filter
finds nothing for an operator id.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from auth.history_principal import history_principal_scope
from models.operation_history import OperationHistory as Event
from services.operation_history_writer import record_operation
from tests.integration.test_operation_history_db import history_db, page, seed  # noqa: F401

pytestmark = pytest.mark.asyncio

OPERATOR_ID = "operator:ops@example.com"


async def test_operator_restore_is_stored_and_read_without_an_actor(history_db):  # noqa: F811
    db = history_db
    s = await seed(db)
    with history_principal_scope("operator"):
        record_operation(
            db, operation="context.restored", actor_user_id=OPERATOR_ID, context_id=s.shared
        )
    await db.commit()
    stored = (await db.execute(select(Event))).scalars().one()
    assert stored.actor_user_id is None
    assert stored.principal_kind == "operator"
    [item] = (await page(db, s))["items"]
    assert item["actor_user_id"] is None
    assert item["actor_name"] is None
    assert item["principal_kind"] == "operator"
    assert (await page(db, s, actor=OPERATOR_ID))["total"] == 0
    assert (await page(db, s, query="ops@example.com"))["total"] == 0


async def test_a_stored_operator_actor_is_masked_on_read(history_db):  # noqa: F811
    db = history_db
    s = await seed(db)
    db.add(
        Event(
            id=uuid4(),
            workspace_id=s.workspace,
            context_id=s.shared,
            context_private=False,
            occurred_at=datetime.now(UTC) - timedelta(minutes=1),
            actor_user_id=s.owner,
            principal_kind="operator",
            surface="api",
            operation="context.restored",
            target_type="context",
            outcome="success",
            count=1,
            changed_fields=[],
        )
    )
    await db.commit()
    [item] = (await page(db, s))["items"]
    assert item["actor_user_id"] is None
    assert item["actor_name"] is None
