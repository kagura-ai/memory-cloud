"""Apply the deployment reranker default to existing context search configs (#1572).

New contexts get ``DEFAULT_USE_RERANK`` / ``DEFAULT_RERANKER_PROVIDER`` /
``DEFAULT_RERANKER_MODEL`` at creation; rows created before the deployment set
those (or under an older default) keep the code default (off / voyage /
rerank-2). Migrations never rewrite rows (#1207), so this one-shot command does
it deliberately. Runs where the API runs (same env: DATABASE_URL, RERANK_*,
DEFAULT_*), e.g. inside the API container::

    python -m src.cli.apply_rerank_defaults --all                       # plan (default, read-only)
    python -m src.cli.apply_rerank_defaults --workspace <uuid> --apply --yes

Heuristic: a row whose reranker fields equal the CODE default (use_rerank=false,
reranker_provider=voyage, reranker_model rerank-2 or rerank-2-lite) is treated
as "never chosen" and converted; any other value is an explicit choice and is
left alone. An owner who explicitly picked the old default is indistinguishable
and is converted too — there is no "explicitly set" marker on the row. Running
again after ``--apply`` changes 0 rows.

Exit codes: 0 ok · 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from config.settings import Settings, get_settings  # noqa: E402
from db.base import get_db  # noqa: E402
from models.auth import Context  # noqa: E402
from models.config import ContextSearchConfig  # noqa: E402
from repositories.config_repository import search_config_defaults  # noqa: E402

# What a row carries when nobody ever chose a reranker: the ORM column defaults
# (models/config.py) plus the ``rerank-2-lite`` create_context wrote before #1572.
CODE_DEFAULT_PROVIDER = "voyage"
CODE_DEFAULT_MODELS = frozenset({"rerank-2", "rerank-2-lite"})


@dataclass
class PlanLine:
    """One context's verdict: ``convert`` (code default → deployment default) or ``skip``."""

    context_id: UUID
    action: str
    use_rerank: bool
    reranker_provider: str | None
    reranker_model: str | None


@dataclass
class ApplyResult:
    """Outcome of one pass over the rows in scope."""

    target: dict[str, object]
    dry_run: bool
    lines: list[PlanLine] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.lines)

    @property
    def converted(self) -> int:
        return sum(1 for line in self.lines if line.action == "convert")

    @property
    def skipped(self) -> int:
        return self.scanned - self.converted

    @property
    def converted_ids(self) -> list[UUID]:
        return [line.context_id for line in self.lines if line.action == "convert"]


def is_code_default(row: ContextSearchConfig) -> bool:
    """True when the row still carries the never-chosen code default."""
    return (
        row.use_rerank is False
        and row.reranker_provider == CODE_DEFAULT_PROVIDER
        and row.reranker_model in CODE_DEFAULT_MODELS
    )


async def apply_rerank_defaults(
    db: AsyncSession,
    *,
    workspace_id: UUID | None,
    dry_run: bool = True,
    settings: Settings | None = None,
) -> ApplyResult:
    """Convert code-default rows in scope to the deployment default.

    Rows already equal to the target, and rows carrying any explicit (non
    code-default) choice, are reported as ``skip`` and never touched. The
    function is idempotent: a second run after ``dry_run=False`` converts 0.

    Args:
        db: Async session; committed only when ``dry_run`` is False and at
            least one row changed.
        workspace_id: Narrow to one workspace's live contexts; ``None`` = all.
        dry_run: Plan only — nothing is written.
        settings: Settings the target is derived from (defaults to the
            process settings; injectable for tests).

    Returns:
        ApplyResult with the target values and one PlanLine per row scanned.
    """
    target = search_config_defaults(settings or get_settings())

    stmt = (
        select(ContextSearchConfig)
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(Context.deleted_at.is_(None))
        .order_by(Context.created_at, Context.id)
    )
    if workspace_id is not None:
        stmt = stmt.where(Context.workspace_id == workspace_id)
    rows = list((await db.execute(stmt)).scalars().all())

    result = ApplyResult(target=target, dry_run=dry_run)
    for row in rows:
        already_target = (
            row.use_rerank == target["use_rerank"]
            and row.reranker_provider == target["reranker_provider"]
            and row.reranker_model == target["reranker_model"]
        )
        action = "convert" if is_code_default(row) and not already_target else "skip"
        result.lines.append(
            PlanLine(
                context_id=row.context_id,
                action=action,
                use_rerank=row.use_rerank,
                reranker_provider=row.reranker_provider,
                reranker_model=row.reranker_model,
            )
        )
        if action == "convert" and not dry_run:
            row.use_rerank = bool(target["use_rerank"])
            row.reranker_provider = str(target["reranker_provider"])
            row.reranker_model = str(target["reranker_model"])

    if not dry_run and result.converted:
        await db.commit()
    return result


def _print_plan(result: ApplyResult) -> None:
    target = result.target
    print(
        f"target  use_rerank={target['use_rerank']} provider={target['reranker_provider']} "
        f"model={target['reranker_model']}"
    )
    for line in result.lines:
        print(
            f"  {line.action:7} {line.context_id}  use_rerank={line.use_rerank} "
            f"provider={line.reranker_provider} model={line.reranker_model}"
        )
    verb = "would convert" if result.dry_run else "converted"
    print(f"{result.scanned} context(s): {verb} {result.converted}, left alone {result.skipped}")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def _main(args: argparse.Namespace) -> int:
    try:
        async for db in get_db():
            plan = await apply_rerank_defaults(db, workspace_id=args.workspace, dry_run=True)
            _print_plan(plan)
            if not args.apply:
                if plan.converted:
                    print("dry run — pass --apply to write")
                return 0
            if not plan.converted:
                return 0
            if not _confirm(f"Convert {plan.converted} context(s)?", args.yes):
                print("  skipped")
                return 0
            applied = await apply_rerank_defaults(db, workspace_id=args.workspace, dry_run=False)
            print(f"converted {applied.converted} context(s)")
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workspace", type=UUID, help="every live context in a workspace")
    scope.add_argument("--all", action="store_true", help="every live context in the deployment")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan", action="store_true", help="print what would change (default, read-only)"
    )
    mode.add_argument(
        "--apply", action="store_true", help="write the deployment default to default-valued rows"
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
