"""Remove old platform objects with no file registry row (#2395).

Run with the API's storage/DATABASE_URL environment. Default: read-only plan.
Prints counts only. --apply --yes repeats all checks before deleting. Deploy
f18_2395_file_cleanup to every writer database before running this command.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._oneshot import configure_logging, run_plan_apply  # noqa: E402
from services.storage_cleanup import sweep_unregistered_objects  # noqa: E402
from storage.factory import get_blob_storage  # noqa: E402
from storage.protocol import BlobInventoryProtocol  # noqa: E402


async def _main(args: argparse.Namespace) -> int:
    # Even failure output must contain no keys (legacy keys can include names).
    configure_logging("ERROR")
    if args.yes and not args.apply:
        print("error: --yes needs --apply", file=sys.stderr)
        return 2
    try:
        storage = cast(BlobInventoryProtocol, get_blob_storage())
    except Exception as exc:  # noqa: BLE001 - factory errors can contain configuration
        print(f"error_class={type(exc).__name__}", file=sys.stderr)
        return 1
    had_failures = False

    async def run(db, dry_run):
        nonlocal had_failures
        try:
            result = await sweep_unregistered_objects(db, storage, dry_run=dry_run)
            had_failures = had_failures or result["failed"] > 0
            return result
        except Exception as exc:  # noqa: BLE001 - sanitize shared CLI error boundary
            raise RuntimeError(type(exc).__name__) from None

    def report(result):
        print(" ".join(f"{name}={count}" for name, count in result.items()))

    try:
        status = await run_plan_apply(
            run=run,
            print_plan=report,
            changes=lambda r: r["candidates"],
            noun="object",
            verb="delete",
            print_applied=report,
            apply=args.apply,
            assume_yes=args.yes,
        )
        return 1 if had_failures else status
    except Exception as exc:  # noqa: BLE001 - no storage keys/exception text
        print(f"error_class={type(exc).__name__}", file=sys.stderr)
        return 1


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
