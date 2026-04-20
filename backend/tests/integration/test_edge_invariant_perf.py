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

Per-run wall time is also printed to stdout for observability — operators
running ``make test-integration`` can eyeball the number across branches
to spot small-but-real regressions that don't trip the assertion.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory
from repositories.neural_edge import NeuralEdgeRepository

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

# Memory count — enough to produce PERF_N_EDGES unique (src, dst) pairs
# without collisions. 10 memories → 90 ordered pairs > 50.
PERF_N_MEMORIES = 10


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
    """Insert PERF_N_EDGES via the repo with the invariant check active."""
    repo = NeuralEdgeRepository(db_session)
    s = perf_scenario
    mems = s["memories"]

    # Build unique (src, dst) pairs up front — done outside the timed loop so
    # Python overhead doesn't leak into the measurement.
    pairs: list[tuple] = []
    for i in range(PERF_N_MEMORIES):
        for j in range(PERF_N_MEMORIES):
            if i == j:
                continue
            pairs.append((mems[i].id, mems[j].id))
            if len(pairs) == PERF_N_EDGES:
                break
        if len(pairs) == PERF_N_EDGES:
            break

    # Warm up — avoids first-call fixed overhead (connection priming, prepared
    # statement caching) dominating the per-op average on a small N run.
    for src_id, dst_id in pairs[:3]:
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=src_id,
            dst_id=dst_id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_id"]),
        )

    t0 = time.perf_counter()
    for src_id, dst_id in pairs:
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

    # Print is the observability channel — appears in pytest output for operators
    # to eyeball across branches. Not a structured metric by design.
    print(
        f"\n[perf] {PERF_N_EDGES} edge inserts through invariant check: "
        f"{total_ms:.1f}ms total, {avg_ms:.2f}ms avg"
    )

    assert avg_ms < PERF_AVG_MS_THRESHOLD, (
        f"avg latency {avg_ms:.2f}ms exceeds {PERF_AVG_MS_THRESHOLD}ms threshold — "
        f"invariant check may have regressed (missing index, chatty query, N+1). "
        f"Total: {total_ms:.1f}ms over {PERF_N_EDGES} inserts."
    )
