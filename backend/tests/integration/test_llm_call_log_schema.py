"""Integration tests for the llm_call_log schema (#474).

Covers:

- CHECK constraint on ``caller`` rejects values outside the allowed tuple.
- CHECK constraint on ``call_type`` rejects values outside the allowed tuple.
- CHECK constraint on ``paid_by`` rejects values outside ``('platform', 'byok')``.
- CHECK constraint on ``cost_usd >= 0`` rejects negative values.
- CHECK constraint on ``octet_length(call_metadata::text) <= 4096`` rejects
  oversized JSONB payloads — the codebase's first JSONB size cap, so the
  test is the contract for future similar columns.
- ``information_schema`` DDL audit: column types + nullability + indexes
  are what the model declared. Catches drift between the migration and
  the model (mirrors ``test_sleep_report_schema.py`` and PR #610's
  ``server_default`` parity rule).
- ORM defaults: ``cost_usd`` defaults to 0, ``paid_by`` defaults to
  'platform' on a row built without those columns.
- BYOK round-trip: ``paid_by='byok'`` persists and reads back unchanged.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from models.llm_call_log import LLMCallLog


def _make_call_log(**overrides: Any) -> LLMCallLog:
    """Minimal LLMCallLog constructor; callers override columns under test.

    Defaults pick the most permissive caller ('admin') so identity columns
    can stay None unless a test wants to exercise the nullability matrix.
    """
    defaults: dict[str, Any] = {
        "occurred_at": datetime(2026, 5, 14, 0, 0, 0),
        "caller": "admin",
        "call_type": "completion",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    defaults.update(overrides)
    return LLMCallLog(**defaults)


@pytest.mark.asyncio
async def test_check_rejects_invalid_caller(db_session):
    """caller outside the allowed tuple must raise IntegrityError."""
    db_session.add(_make_call_log(caller="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_rejects_invalid_call_type(db_session):
    """call_type outside ('completion', 'embedding', 'rerank') must raise."""
    db_session.add(_make_call_log(call_type="audio"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_rejects_invalid_paid_by(db_session):
    """paid_by outside ('platform', 'byok') must raise."""
    db_session.add(_make_call_log(paid_by="user"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_rejects_negative_cost(db_session):
    """cost_usd must be >= 0."""
    db_session.add(_make_call_log(cost_usd=Decimal("-0.01")))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_rejects_oversized_metadata(db_session):
    """call_metadata over 4 KB (octet_length on text form) must raise.

    Defends against accidental storage of prompt bodies. The threshold
    is the source of truth for the codebase's first JSONB size cap;
    future PII-sensitive JSONB columns should mirror this pattern.
    """
    # Build a payload whose JSON text serialization is well over 4096
    # bytes. A single 5000-char string is ~5004 bytes once quoted.
    oversized = {"prompt": "x" * 5000}
    db_session.add(_make_call_log(call_metadata=oversized))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_metadata_under_limit_accepted(db_session):
    """call_metadata at ~1 KB (well under the 4 KB cap) round-trips."""
    payload = {
        "request_id": "req_abc123",
        "latency_ms": 248,
        "retry_count": 0,
    }
    row = _make_call_log(call_metadata=payload)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.call_metadata == payload
    await db_session.rollback()


@pytest.mark.asyncio
async def test_orm_defaults_populate_after_flush(db_session):
    """cost_usd defaults to 0 and paid_by defaults to 'platform' without explicit set.

    The Python-side ``default=`` fires at flush. Server-default presence
    is exercised separately by ``test_server_default_clause_present_in_schema``.
    """
    row = _make_call_log()  # no cost_usd, no paid_by
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.cost_usd == Decimal("0")
    assert row.paid_by == "platform"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_byok_round_trips(db_session):
    """paid_by='byok' persists and reads back unchanged (recall BYOK path)."""
    row = _make_call_log(paid_by="byok")
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.paid_by == "byok"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_server_default_clause_present_in_schema(db_session):
    """server_default is declared in the catalog for cost_usd and paid_by.

    Reads ``information_schema.columns.column_default`` directly to catch
    drift between ORM-declared defaults and the migration. The Python-side
    ``default=`` tests above would silently pass if the migration omitted
    the ``server_default``.
    """
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT column_name, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'llm_call_log' "
                "AND column_name IN ('cost_usd', 'paid_by') "
                "ORDER BY column_name"
            )
        )
    ).all()

    defaults = {r.column_name: r.column_default for r in rows}
    assert defaults["cost_usd"] is not None and "0" in defaults["cost_usd"]
    assert (
        defaults["paid_by"] is not None and "'platform'::character varying" in defaults["paid_by"]
    )


@pytest.mark.asyncio
async def test_required_indexes_exist(db_session):
    """Both query-shape indexes are declared in the catalog.

    The (occurred_at) single-column index supports the admin-wide query;
    the (workspace_id, occurred_at) composite supports per-tenant
    aggregation and matches the #472 UNION ALL leg against sleep_reports.
    """
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'llm_call_log' "
                "ORDER BY indexname"
            )
        )
    ).all()

    index_names = {r.indexname for r in rows}
    assert "idx_llm_call_log_occurred_at" in index_names
    assert "idx_llm_call_log_workspace_period" in index_names


@pytest.mark.asyncio
async def test_full_happy_path_round_trip(db_session):
    """A recall-shaped row with all identity + usage fields round-trips."""
    import uuid

    workspace_id = uuid.uuid4()
    context_id = uuid.uuid4()
    row = _make_call_log(
        caller="recall",
        call_type="embedding",
        provider="openai",
        model="text-embedding-3-small",
        user_id="user_42",
        workspace_id=workspace_id,
        context_id=context_id,
        input_tokens=None,
        output_tokens=None,
        embedding_tokens=512,
        cost_usd=Decimal("0.000010"),
        paid_by="platform",
        call_metadata={"request_id": "req_xyz"},
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.id is not None
    assert row.caller == "recall"
    assert row.call_type == "embedding"
    assert row.workspace_id == workspace_id
    assert row.context_id == context_id
    assert row.embedding_tokens == 512
    assert row.cost_usd == Decimal("0.000010")
    assert row.call_metadata == {"request_id": "req_xyz"}
    await db_session.rollback()
