"""Perf observability for the edge context invariant check (#396 AC 6 follow-up).

The write-path ``_validate_edge_context_invariant`` added in the sibling
commit adds one ``SELECT`` against ``memories`` per edge insert. This is a
cheap PK lookup that should stay sub-ms, but the check sits on the Hebbian
hot path — ``GraphService.add_edge`` is called once per updated edge in
``HebbianLearner.apply_updates``, which processes hundreds of pairs per
sleep run. A regression that turned this into a slow query (missing index,
accidental full-table scan, chatty driver round-trips) would pile up.

This test is an **alarm bell**, not a tight perf budget: it inserts a
batch of edges through the full repo path and asserts the average latency
stays under a generous threshold. Local Postgres is typically < 3 ms/op;
the threshold is set high enough to survive slow CI runners while still
catching an order-of-magnitude regression.

Per-run wall time is emitted through ``logging`` rather than ``print`` so
pytest's default output capture does not swallow it on passing runs. To
surface the number in normal pytest output:

    pytest tests/integration/test_edge_invariant_perf.py --log-cli-level=INFO

Or set ``log_cli = true`` + ``log_cli_level = INFO`` in ``pytest.ini`` /
``pyproject.toml`` for repo-wide default visibility. On failure the number
appears in the captured log regardless.
"""

from __future__ import annotations

import logging
import os
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory
from repositories.neural_edge import NeuralEdgeRepository

perf_logger = logging.getLogger(__name__)

# Generous threshold — local Postgres measures ~1 ms/op end-to-end (two
# round-trips: invariant SELECT + upsert). CI runners with slow I/O may
# hit 10-20ms. A regression into the 100+ ms range would indicate a
# real correctness-affecting pathology (missing index, N+1 inside the
# check, retries). Tune only if consistently flaky across green CI runs.
PERF_AVG_MS_THRESHOLD = 50.0

# Number of edges to insert per perf run. Small enough to stay under the
# pytest-timeout=120 budget on slow CI; large enough to smooth out per-op
# jitter (one 200-ms GC pause in a 5-sample run would skew the average).
PERF_N_EDGES = 50

# Warm-up edge count — uses a DISJOINT set of (src, dst) pairs from the
# timed loop so the timed operations are all inserts (not upserts that
# hit ON CONFLICT DO UPDATE). Overlapping warm-up with timed would hide
# insert-path regressions because the UPDATE path is cheaper.
PERF_N_WARMUP = 3

# Memory count — enough to produce PERF_N_EDGES + PERF_N_WARMUP disjoint
# (src, dst) ordered pairs. 10 memories → 90 ordered pairs, comfortably
# above the 50 + 3 = 53 needed.
PERF_N_MEMORIES = 10

# Opt-in guard. Wall-clock perf assertions on shared CI runners are prone
# to noisy-neighbor flakes, which erode trust in the signal and encourage
# operators to ignore or xfail the test. Gate with an env var so the full
# suite (make test-integration, CI default) logs the number but never
# blocks on it; dedicated perf runs can opt in via
# ``RUN_PERF_TESTS=1 pytest tests/integration/test_edge_invariant_perf.py``.
PERF_OPT_IN = os.getenv("RUN_PERF_TESTS") == "1"


@pytest_asyncio.fixture
async def perf_scenario(db_session: AsyncSession):
    """One workspace + one context + N memories, all in the same context."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )

    memories = [
        Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"m{i}",
            content="x",
            type="note",
            client="test",
        )
        for i in range(PERF_N_MEMORIES)
    ]

    # Same 3-step flush pattern as test_edge_context_invariant / test_graph_visibility —
    # Context and Memory lack back-populating relationship() so UoW cannot topo-sort.
    db_session.add(ws)
    await db_session.flush()

    db_session.add(ctx)
    await db_session.flush()

    db_session.add_all(memories)
    await db_session.flush()

    yield {
        "owner_id": owner_id,
        "ws_id": ws.id,
        "ctx_id": ctx.id,
        "memories": memories,
    }

    await db_session.rollback()


@pytest.mark.asyncio
async def test_invariant_check_throughput_stays_under_threshold(
    db_session: AsyncSession, perf_scenario
):
    """Insert PERF_N_EDGES via the repo with the invariant check active.

    Always logs the per-run wall-clock number so operators can eyeball it
    across branches. The threshold assertion only fires when the
    ``RUN_PERF_TESTS=1`` env var is set — on shared CI runners the
    wall-clock assertion is flake-prone, so it is opt-in to avoid eroding
    signal-to-noise in the default suite. A regression would still be
    visible in the logged number even without the assertion.
    """
    repo = NeuralEdgeRepository(db_session)
    s = perf_scenario
    mems = s["memories"]

    # Build unique (src, dst) pairs up front — done outside the timed loop so
    # Python overhead doesn't leak into the measurement. Warm-up pairs and
    # timed pairs are DISJOINT so the timed path exercises inserts, not the
    # ON CONFLICT DO UPDATE branch that warm-up already filled.
    pairs: list[tuple] = []
    needed = PERF_N_EDGES + PERF_N_WARMUP
    for i in range(PERF_N_MEMORIES):
        for j in range(PERF_N_MEMORIES):
            if i == j:
                continue
            pairs.append((mems[i].id, mems[j].id))
            if len(pairs) == needed:
                break
        if len(pairs) == needed:
            break

    warmup_pairs = pairs[:PERF_N_WARMUP]
    timed_pairs = pairs[PERF_N_WARMUP : PERF_N_WARMUP + PERF_N_EDGES]
    assert len(timed_pairs) == PERF_N_EDGES  # sanity: enough distinct pairs generated

    # Warm up — avoids first-call fixed overhead (connection priming, prepared
    # statement caching, lazy schema metadata) dominating the per-op average
    # on a small N run. Uses pairs that will NOT appear in the timed loop.
    for src_id, dst_id in warmup_pairs:
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=src_id,
            dst_id=dst_id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_id"]),
        )

    t0 = time.perf_counter()
    for src_id, dst_id in timed_pairs:
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=src_id,
            dst_id=dst_id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_id"]),
        )
    elapsed_s = time.perf_counter() - t0

    total_ms = elapsed_s * 1000
    avg_ms = total_ms / PERF_N_EDGES

    # Log (not print) so pytest's default capture does not swallow the number
    # on passing runs. Operators see it with --log-cli-level=INFO. On failure
    # the assertion message below carries the same figures, so the signal is
    # never lost even when logs are silenced.
    perf_logger.info(
        "[perf] %d edge inserts through invariant check: %.1fms total, %.2fms avg",
        PERF_N_EDGES,
        total_ms,
        avg_ms,
    )

    if not PERF_OPT_IN:
        # Default suite path: observe the number, do not gate on it. If the
        # logged average drifts into the tens or hundreds of ms on your
        # runs, re-run with RUN_PERF_TESTS=1 to turn on the assertion and
        # see a crisp failure message.
        return

    assert avg_ms < PERF_AVG_MS_THRESHOLD, (
        f"avg latency {avg_ms:.2f}ms exceeds {PERF_AVG_MS_THRESHOLD}ms threshold — "
        f"invariant check may have regressed (missing index, chatty query, N+1). "
        f"Total: {total_ms:.1f}ms over {PERF_N_EDGES} inserts."
    )
