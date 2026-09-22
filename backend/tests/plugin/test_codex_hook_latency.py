"""PreToolUse latency probe for the Codex guardrail hook (#1620).

Skipped unless ``KAGURA_HOOK_BENCH=1``. Runs the exact Codex ``hooks.json``
command 200 times over a 50-entry cache and asserts the script-alone p95 stays
at or below 50 ms (the merge gate). The whole-run number is measured under
``sh -c`` here; Codex itself runs ``$SHELL -lc``, whose login-shell startup is
machine-specific and is reported from the manual run, without a target.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time

import pytest

from tests.plugin.conftest import HOOK_SCRIPT, item
from tests.plugin.test_guardrail_hooks_codex import (  # noqa: F401 - codex_env is a fixture
    CodexEnv,
    bash,
    codex_command,
    codex_env,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("KAGURA_HOOK_BENCH") != "1", reason="set KAGURA_HOOK_BENCH=1 to run the probe"
)

RUNS = 200
P95_GATE_MS = 50.0

COMMANDS = [
    "git status --short",
    "ls -la src/",
    "pytest tests/api -q",
    "gh pr view 12 --json state",
    "rg -n 'def main' backend/src",
    "cat README.md | head -20",
    "docker compose logs --tail 20 api",
    "npm run build",
]


def _cache_items() -> list[dict]:
    patterns = [
        (r"gh pr merge\b.*--delete-branch", "inform"),
        (r"\bps\b|\bpgrep\b", "block"),
        (r"git push\s+--force", "block"),
        (r"rm -rf /", "block"),
        (r"(?i)docker system prune", "inform"),
        (r"\.env$", "inform"),
        (r"alembic downgrade", "inform"),
        (r"npm publish", "block"),
        (r"pip install .*--break-system-packages", "inform"),
        (r"curl .*\| *sh", "block"),
    ]
    return [
        item(
            n + 1,
            f"guardrail {n}: prefer the safe alternative",
            "Bash" if n % 5 else "Edit|Write",
            match=patterns[n % len(patterns)][0],
            action=patterns[n % len(patterns)][1],
        )
        for n in range(50)
    ]


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def test_codex_pre_tool_use_p95(codex_env: CodexEnv) -> None:  # noqa: F811 - fixture
    codex_env.write_cache(_cache_items())
    command = codex_command("PreToolUse")
    script_ms: list[float] = []
    whole_ms: list[float] = []
    for i in range(RUNS):
        body = json.dumps(bash(COMMANDS[i % len(COMMANDS)])).encode()
        started = time.perf_counter()
        proc = subprocess.run(
            ["python3", "-I", "-S", str(HOOK_SCRIPT), "--client", "codex"],
            input=body,
            capture_output=True,
            cwd=codex_env.project_dir,
            env=codex_env.env,
            check=False,
        )
        script_ms.append((time.perf_counter() - started) * 1000)
        assert proc.returncode == 0 and proc.stdout == b""
        started = time.perf_counter()
        proc = subprocess.run(
            ["sh", "-c", command],
            input=body,
            capture_output=True,
            cwd=codex_env.project_dir,
            env=codex_env.env,
            check=False,
        )
        whole_ms.append((time.perf_counter() - started) * 1000)
        assert proc.returncode == 0
    report = (
        f"script-alone p50={statistics.median(script_ms):.1f}ms p95={_percentile(script_ms, 95):.1f}ms; "
        f"whole-run (sh guard) p50={statistics.median(whole_ms):.1f}ms p95={_percentile(whole_ms, 95):.1f}ms"
    )
    print("\nKAGURA_HOOK_BENCH codex " + report)
    assert _percentile(script_ms, 95) <= P95_GATE_MS, report
