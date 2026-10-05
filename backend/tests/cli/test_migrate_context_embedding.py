"""Tests for ``cli/migrate_context_embedding``: the UNREBUILDABLE report (#1896).

The migration is covered in ``tests/services/test_embedding_migration_service.py``
and ``tests/services/test_embedding_migration_resource_rows.py``; this pins
what the command prints about resource-ingested memories it could not rebuild.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

# The CLI bootstraps sys.path itself; import it the same way create_admin's test does.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import migrate_context_embedding as cli  # noqa: E402
from services.embedding_migration_service import (  # noqa: E402
    MigrationPlan,
    ReembedResult,
    VerifyResult,
)

_MODEL = "qwen3-embedding:4b"


def _plan(context_id: UUID) -> MigrationPlan:
    return MigrationPlan(
        context_id=context_id,
        workspace_id=uuid4(),
        source_model="text-embedding-3-small",
        source_dimensions=512,
        source_collection="kagura_memories",
        target_model=_MODEL,
        target_dimensions=2560,
        target_collection="kagura_memories_qwen3_embedding_4b_2560",
        memory_count=3,
    )


async def _run(
    argv: list[str], *, reembed: ReembedResult, verify: VerifyResult | None = None
) -> int:
    context_id = uuid4()
    args = cli._parse(["--to", _MODEL, "--context", str(context_id), *argv])
    with (
        patch.object(cli, "plan_context_migration", AsyncMock(return_value=_plan(context_id))),
        patch.object(cli, "reembed_context", AsyncMock(return_value=reembed)),
        patch.object(cli, "verify_context_migration", AsyncMock(return_value=verify)),
    ):
        return await cli._one_context(MagicMock(), context_id, args)


def _reembed(unrebuildable: list[UUID]) -> ReembedResult:
    return ReembedResult(
        embedded=2, batches=1, started_at=datetime(2026, 10, 1), unrebuildable=unrebuildable
    )


@pytest.mark.asyncio
async def test_reembed_alone_lists_the_rows_it_could_not_rebuild(capsys):
    skipped = uuid4()
    assert await _run(["--reembed"], reembed=_reembed([skipped])) == 0
    out = capsys.readouterr().out
    assert out.count("UNREBUILDABLE 1 ") == 1
    assert str(skipped) in out


@pytest.mark.asyncio
async def test_reembed_and_verify_report_every_skipped_row_once(capsys):
    # ``kept`` was skipped by the re-embed, but another live row keeps its
    # point in the target, so verify finds it present and does not list it.
    both, kept = uuid4(), uuid4()
    verified = VerifyResult(expected=3, present=2, unrebuildable=[both])
    code = await _run(["--reembed", "--verify"], reembed=_reembed([both, kept]), verify=verified)
    out = capsys.readouterr().out
    # Unrebuildable rows are not missing rows: the step does not fail.
    assert code == 0
    assert out.count("UNREBUILDABLE") == 1
    assert "UNREBUILDABLE 2 " in out
    assert out.count(str(both)) == 1 and out.count(str(kept)) == 1


@pytest.mark.asyncio
async def test_nothing_is_printed_when_every_row_was_rebuilt(capsys):
    verified = VerifyResult(expected=3, present=3)
    assert await _run(["--reembed", "--verify"], reembed=_reembed([]), verify=verified) == 0
    assert "UNREBUILDABLE" not in capsys.readouterr().out
