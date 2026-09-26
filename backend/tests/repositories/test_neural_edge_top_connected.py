"""``NeuralEdgeRepository.get_top_connected_nodes`` (#1695).

The query aggregates a ``UNION ALL`` of out- and in-degree counts. It used to
read the union's columns through ``CompoundSelect.c``, an implicit-subquery
shortcut SQLAlchemy deprecated in 1.4 and removed in 2.1 — building the
statement raised ``AttributeError`` before it reached the database, so the
graph "top nodes" read failed on every call.

The result against a real database is pinned in
``tests/integration/test_neural_edge_top_connected_db.py``, which the CI
integration job (the one with Postgres) runs.
"""

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from repositories.neural_edge import NeuralEdgeRepository


@pytest.mark.asyncio
async def test_statement_builds_and_aggregates_the_union(mocker) -> None:
    """No DB needed: the failure was in statement construction."""
    db = mocker.AsyncMock()
    db.execute.return_value = mocker.MagicMock(all=mocker.MagicMock(return_value=[]))
    repo = NeuralEdgeRepository(db)

    result = await repo.get_top_connected_nodes(
        workspace_id=str(uuid4()), context_id=str(uuid4()), limit=5
    )

    assert result == []
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
    assert "union all" in sql
    assert "sum(" in sql
    assert "group by" in sql
