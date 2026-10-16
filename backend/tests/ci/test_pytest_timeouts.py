"""Every backend pytest invocation in CI carries a per-test timeout (#1988).

A hung test (a Redis rate-limit path once held ``backend-unit`` for ~50 min;
a cross-loop asyncpg terminate held ``backend-integration`` for 120 s) must
fail in minutes, not run until the job's wall clock expires.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# backend/tests/ci/test_pytest_timeouts.py -> ci -> tests -> backend -> repo root
CI_YML = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"

# Inside the api container the tests sit at /app/tests and the workflow file
# is not mounted; the guard runs from a repository checkout (same shape as
# tests/test_uv_version_lockstep.py).
pytestmark = pytest.mark.skipif(
    not CI_YML.is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)


def _pytest_invocations(text: str) -> list[str]:
    """Each ``run:`` block that invokes pytest, folded onto one line.

    Any ``run:`` block counts — ``cd backend && pytest``, ``uv run pytest`` or
    a job with ``working-directory: backend`` — so a new job cannot slip past
    the guard by spelling the invocation differently.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\s+run:", line):
            if current:
                blocks.append(" ".join(current))
            current = [line]
        elif current and (re.match(r"^\s{10,}\S", line) or not line.strip()):
            current.append(line.strip())
        elif current:
            blocks.append(" ".join(current))
            current = []
    if current:
        blocks.append(" ".join(current))
    return [b for b in blocks if re.search(r"\bpytest\b", b)]


def test_ci_backend_pytest_jobs_have_a_per_test_timeout() -> None:
    text = CI_YML.read_text()
    invocations = _pytest_invocations(text)
    # backend-unit (one line) and backend-integration (a folded ``run: >-``
    # block) at least; a parser that stopped matching one of them must fail
    # here rather than pass on the other alone.
    assert len(invocations) >= 2, invocations
    missing = [inv for inv in invocations if "--timeout=" not in inv]
    assert missing == [], "pytest invocations without --timeout:\n" + "\n".join(missing)
