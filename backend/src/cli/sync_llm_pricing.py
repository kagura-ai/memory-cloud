"""Materialize ``LLM_PRICING_OVERRIDES`` into ``llm_pricing`` (#1570).

The API does this once at startup; run it here to apply a changed price
without a restart, or to see what a restart would insert. Runs where the API
runs (same env: DATABASE_URL, LLM_PRICING_OVERRIDES), e.g. inside the API
container::

    python -m src.cli.sync_llm_pricing            # --plan: print, write nothing
    python -m src.cli.sync_llm_pricing --apply    # append the differing rows

Rows are only ever appended (``effective_from = now``); nothing is updated or
deleted. Other API workers pick the new price up within the 60-minute price
cache TTL — restart them for an immediate effect.

Exit codes: 0 ok · 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import get_settings  # noqa: E402
from db.base import get_db  # noqa: E402
from services.llm_pricing_service import SyncResult, sync_llm_pricing_overrides  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


def _print_result(result: SyncResult, *, applied: bool) -> None:
    verb = "inserted" if applied else "would insert"
    for override in result.inserted:
        print(
            f"  {verb}  {override.provider}/{override.model} {override.unit_type}"
            f"  {override.price_per_unit} USD per {override.unit_denominator}"
            f"  (context_min_tokens={override.context_min_tokens})"
        )
    for override in result.unchanged:
        print(
            f"  unchanged {override.provider}/{override.model} {override.unit_type}"
            f"  {override.price_per_unit} USD per {override.unit_denominator}"
        )
    print(f"{verb}: {len(result.inserted)}, unchanged: {len(result.unchanged)}")


async def _main(args: argparse.Namespace) -> int:
    overrides = get_settings().parsed_llm_pricing_overrides
    if not overrides:
        print("LLM_PRICING_OVERRIDES is empty; nothing to sync")
        return 0
    print(f"{len(overrides)} override(s)")
    async for db in get_db():
        result = await sync_llm_pricing_overrides(
            db, overrides, now=utcnow(), dry_run=not args.apply
        )
        _print_result(result, applied=args.apply)
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print what would change (default)")
    mode.add_argument("--apply", action="store_true", help="append the differing price rows")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
