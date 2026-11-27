#!/usr/bin/env python3
"""Print the test files of one shard, one per line (#3027).

    pytest_shard.py --shard 2 --total 3 tests/integration/ tests/cli/ tests/smoke/test_all_routes.py

Each root is a directory (searched for ``test_*.py`` / ``*_test.py``) or one
file. Every file found lands in exactly one of the ``--total`` shards: files
are taken largest first and each goes to the shard that is lightest so far
(file size stands in for run time), ties broken by path, so the split depends
only on the tree. An empty shard, or a root that matches nothing, is an error:
a job that ran no test must not pass.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def collect(roots: list[str]) -> list[Path]:
    """Every test file under ``roots``, sorted; a root with none raises."""
    files: set[Path] = set()
    for root in roots:
        path = Path(root)
        if path.is_file():
            found = {path}
        else:
            found = {*path.rglob("test_*.py"), *path.rglob("*_test.py")}
        if not found:
            raise SystemExit(f"pytest_shard: no test file under {root!r}")
        files |= found
    return sorted(files)


def assign(files: list[Path], total: int) -> list[list[Path]]:
    """Split ``files`` into ``total`` shards of about equal summed size."""
    if total < 1:
        raise SystemExit("pytest_shard: --total must be 1 or more")
    shards: list[list[Path]] = [[] for _ in range(total)]
    weights = [0] * total
    for path in sorted(files, key=lambda p: (-p.stat().st_size, p.as_posix())):
        lightest = min(range(total), key=lambda i: (weights[i], i))
        shards[lightest].append(path)
        weights[lightest] += path.stat().st_size
    return [sorted(shard) for shard in shards]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int, required=True, help="1-based shard number")
    parser.add_argument("--total", type=int, required=True, help="number of shards")
    parser.add_argument("roots", nargs="+")
    args = parser.parse_args(argv)
    if not 1 <= args.shard <= args.total:
        raise SystemExit(f"pytest_shard: --shard must be between 1 and {args.total}")
    shard = assign(collect(args.roots), args.total)[args.shard - 1]
    if not shard:
        raise SystemExit(f"pytest_shard: shard {args.shard} of {args.total} is empty")
    sys.stdout.write("".join(f"{path.as_posix()}\n" for path in shard))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
