"""Live retrieval-eval orchestration (Issue #344).

Separated from ``test_retrieval_quality.py`` so it can be driven either by the
skip-guarded pytest test or directly via ``python -m tests.eval.runner`` (the
``make eval-retrieval`` target). All heavy imports (DB, services) are local to
the functions so importing this module is cheap and CI-safe.

Produces ``results/<YYYY-MM-DD>.json`` from a REAL run only — never fabricated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tests.eval.metrics import (
    mean_ndcg_at_k,
    mean_precision_at_k,
    mrr_at_k,
    source_recall_share,
)
from tests.eval.tools.corpus import BUCKETS, Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_RECALL_K = 10
_P_AT = (5, 10)
_MRR_AT = 10
_NDCG_AT = (5, 10)

# Comparison arms (#967): (arm_name, search_mode, neural_enabled).
#
# Order is load-bearing — with ENABLE_NEURAL_MEMORY=true, recall() itself
# performs co-activation tracking + Hebbian updates (graph WRITES), so the
# neural arm must run LAST or it would warm the graph for every arm after it.
# The first three arms are read-only with respect to the neural graph.
#
# All arms run against the SAME ingested corpus: k-NN / tag-co-occurrence
# cold-start seeding happens at embedding time regardless of the env var, so
# the neural arm sees exactly the cold-graph state production sees right after
# ingest. Measuring quality growth as the graph warms with use is out of scope
# here (companion issue #969).
_ARMS: tuple[tuple[str, str, bool], ...] = (
    ("keyword", "keyword", False),  # BM25-only baseline
    ("semantic", "semantic", False),  # dense-vector-only baseline
    ("hybrid", "hybrid", False),  # hybrid scoring, neural boost off
    ("hybrid_neural", "hybrid", True),  # full production posture
)
# The arm mirrored at the results top level (backward-compatible shape) and
# used as the regression-gate reference.
_PRODUCTION_ARM = "hybrid_neural"


def _sudachi_version() -> str:
    """Best-effort Sudachi dictionary version string for the results stamp."""
    try:
        import importlib.metadata as md

        return md.version("sudachidict_core")
    except Exception:  # noqa: BLE001 — version stamp is best-effort
        return "unknown"


async def _ingest_corpus(
    svc: Any, corpus: Corpus, user_id: str, ctx_id, ws_id, id_map: dict[str, str]
) -> None:
    """Ingest every corpus doc as a memory, populating ``id_map`` (memory_id →
    corpus_doc_id) INCREMENTALLY as each doc lands.

    The caller owns ``id_map`` so that if this raises partway (a remember /
    indexing failure), teardown still sees the memories created so far and can
    clean them up — no leaked rows on a partial ingest.

    ``remember()`` schedules embedding + Qdrant upsert as a fire-and-forget
    ``asyncio.create_task`` and returns before indexing completes. For a
    deterministic eval we must NOT recall before the docs are searchable, so we
    drive ``process_pending_embedding`` and then poll ``embedding_status`` to a
    terminal state for each memory here — turning the async write into a
    synchronous "ingested AND indexed" barrier.
    """
    from models.schemas import RememberRequest
    from services.memory_service import process_pending_embedding

    for doc in corpus.documents:
        # summary must be >= 10 chars; corpus docs comfortably exceed that.
        req = RememberRequest(
            summary=doc.text[:480],
            content=doc.text,
            type="note",
            tags=[f"evaldoc:{doc.id}", f"evalsrc:{doc.source}"],
        )
        resp = await svc.remember(
            request=req,
            user_id=user_id,
            client="eval-harness",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        # remember() has already COMMITTED the Postgres memory row; the embedding
        # + Qdrant upsert is what process_pending_embedding does below (remember
        # also schedules it as a background task). Record the id BEFORE awaiting
        # that step, so if it raises, teardown's forget() can still clean up the
        # committed PG row (and any partial Qdrant points).
        id_map[str(resp.memory_id)] = doc.id
        # Drive the embedding/Qdrant upsert before any recall runs. This call is
        # claim-based: if the background task remember() fired wins the claim, our
        # call returns immediately WITHOUT the upsert being done. So we then poll
        # embedding_status to a terminal state — only that guarantees the doc is
        # searchable, regardless of which task did the work.
        await process_pending_embedding(resp.memory_id)
        await _await_indexed(svc, resp.memory_id)


async def _await_indexed(
    svc: Any, memory_id: UUID, *, attempts: int = 300, interval_s: float = 0.1
) -> None:
    """Block until ``memory_id``'s embedding reaches a terminal state.

    ``process_pending_embedding`` is claim-based, so the explicit call in
    ``_ingest_corpus`` can return before the Qdrant upsert finishes when
    ``remember()``'s background task wins the claim. We poll ``embedding_status``
    (a column-only SELECT, so it reads committed state past the session identity
    map under READ COMMITTED) until it is terminal.

    A ``failed`` status is an ERROR, not a stop condition: the failed memory was
    never upserted to Qdrant, so it is unsearchable and recalls against it would
    silently understate metrics. Both ``failed`` and timeout raise, so the eval
    never runs against a half-indexed corpus.
    """
    import asyncio

    from sqlalchemy import select

    from models.memory import Memory

    for _ in range(attempts):
        row = (
            await svc.db.execute(
                select(Memory.embedding_status, Memory.embedding_error).where(
                    Memory.id == memory_id
                )
            )
        ).first()
        status = row[0] if row else None
        if status == "success":
            return
        if status == "failed":
            raise RuntimeError(
                f"embedding FAILED for memory {memory_id}: {row[1]} — that doc is "
                "not in Qdrant, so eval would run against a half-indexed corpus"
            )
        await asyncio.sleep(interval_s)
    raise RuntimeError(
        f"embedding for memory {memory_id} never reached a terminal state "
        f"within {attempts * interval_s:.0f}s — eval would run against a "
        "half-indexed corpus"
    )


async def run_retrieval_eval(write: bool = True, run_date: str | None = None) -> dict[str, Any]:
    """Ingest the corpus, run every query, compute metrics, write results JSON.

    Args:
        write: when True, persist results/<run_date>.json.
        run_date: YYYY-MM-DD label for the results file; defaults to today (UTC).

    Returns the results dict (also written to disk when ``write``).
    """
    from auth.workspace_roles import WorkspaceRole
    from db.base import get_db
    from models.auth import Context, Workspace, WorkspaceMember
    from services.memory_service import MemoryService
    from utils.datetime import utcnow

    corpus = load_corpus()
    docs_by_id = corpus.docs_by_id
    run_date = run_date or utcnow().strftime("%Y-%m-%d")

    async for db in get_db():
        owner = f"eval_{uuid4().hex[:8]}"
        ws = Workspace(
            id=uuid4(),
            name=f"eval-ws-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=10_000_000,
            weekly_api_limit=50_000_000,
        )
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"eval-ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        db.add(ws)
        await db.flush()
        db.add(ctx)
        # remember()/recall() resolve the context via PermissionService, which
        # checks workspace_members — owner_user_id alone is NOT a membership, so
        # without this row get_context raises NotFoundException on first ingest.
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        await db.flush()
        await db.commit()

        svc = MemoryService(db)
        # id_map is owned here and populated incrementally by _ingest_corpus, so
        # the finally below ALWAYS cleans up whatever was created — even if
        # ingest itself raises partway through (no leaked workspace/context/rows).
        id_map: dict[str, str] = {}
        try:
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)
            return await _run_queries_and_score(
                svc, corpus, docs_by_id, id_map, owner, ctx.id, ws.id, run_date, write
            )
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))

    raise RuntimeError("database session unavailable — is the stack up?")


async def _teardown(
    svc: Any, db: Any, owner: str, ctx_id: Any, ws_id: Any, memory_ids: list[str]
) -> None:
    """Remove the throwaway eval data so repeated runs don't accumulate
    workspaces / Qdrant points / DB rows (best-effort, never raises)."""
    from sqlalchemy import delete as _delete

    from models.auth import Context, Workspace, WorkspaceMember
    from models.schemas import ForgetRequest

    for mid in memory_ids:
        try:
            # forget() soft-deletes in Postgres (sets deleted_at/deleted_by) and
            # hard-deletes the Qdrant point. The PG row is physically removed below
            # by the Context DELETE via ON DELETE CASCADE.
            await svc.forget(ForgetRequest(memory_id=UUID(mid)), owner, ctx_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            # forget() shares this session; a mid-op failure can leave it needing
            # a rollback, which would poison the Workspace/Context DELETEs below.
            # Roll back so the rest of teardown still runs.
            await db.rollback()
    try:
        await db.execute(_delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id))
        await db.execute(_delete(Context).where(Context.id == ctx_id))
        await db.execute(_delete(Workspace).where(Workspace.id == ws_id))
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        await db.rollback()


def _bucket_metrics(rankings: list[tuple[list[str], set[str]]]) -> dict[str, Any]:
    return {
        "n": len(rankings),
        **{f"p@{k}": round(mean_precision_at_k(rankings, k), 4) for k in _P_AT},
        f"mrr@{_MRR_AT}": round(mrr_at_k(rankings, _MRR_AT), 4),
        **{f"ndcg@{k}": round(mean_ndcg_at_k(rankings, k), 4) for k in _NDCG_AT},
    }


async def _score_arm(
    svc: Any,
    corpus: Corpus,
    docs_by_id: dict[str, Any],
    id_map: dict[str, str],
    owner: str,
    ctx_id: Any,
    ws_id: Any,
    search_mode: str,
) -> dict[str, Any]:
    """Recall every query with ``search_mode`` and compute the metric block."""
    from models.schemas import RecallRequest

    # Per-query rankings, grouped by bucket.
    per_bucket_rankings: dict[str, list[tuple[list[str], set[str]]]] = {b: [] for b in BUCKETS}
    all_rankings: list[tuple[list[str], set[str]]] = []
    # Source label of every retrieved top-k result across ALL queries — the
    # overall memory-vs-resource share is computed over this whole pool (NOT
    # sliced to a single k window, which would reflect only one query).
    all_retrieved_sources: list[str] = []

    for q in corpus.queries:
        resp = await svc.recall(
            request=RecallRequest(query=q.text, k=_RECALL_K, search_mode=search_mode),
            user_id=owner,
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        ranked_docs = [id_map.get(str(r.memory_id), "?") for r in resp.results]
        relevant = set(q.relevant)
        per_bucket_rankings[q.bucket].append((ranked_docs, relevant))
        all_rankings.append((ranked_docs, relevant))
        all_retrieved_sources.extend(
            docs_by_id[d].source for d in ranked_docs[:_RECALL_K] if d in docs_by_id
        )

    return {
        "overall": _bucket_metrics(all_rankings),
        "per_bucket": {b: _bucket_metrics(per_bucket_rankings[b]) for b in BUCKETS},
        "source_recall@10": {
            k: round(v, 4)
            for k, v in source_recall_share(
                all_retrieved_sources, len(all_retrieved_sources)
            ).items()
        },
    }


async def _run_queries_and_score(
    svc: Any,
    corpus: Corpus,
    docs_by_id: dict[str, Any],
    id_map: dict[str, str],
    owner: str,
    ctx_id: Any,
    ws_id: Any,
    run_date: str,
    write: bool,
) -> dict[str, Any]:
    """Score every comparison arm, compute metrics, optionally write results JSON.

    ``ENABLE_NEURAL_MEMORY`` is toggled per arm (recall() reads it on every
    call) and restored afterwards so the surrounding process env is untouched.
    """
    arms: dict[str, dict[str, Any]] = {}
    prev_neural = os.environ.get("ENABLE_NEURAL_MEMORY")
    try:
        for arm_name, search_mode, neural_enabled in _ARMS:
            os.environ["ENABLE_NEURAL_MEMORY"] = "true" if neural_enabled else "false"
            arms[arm_name] = await _score_arm(
                svc, corpus, docs_by_id, id_map, owner, ctx_id, ws_id, search_mode
            )
    finally:
        if prev_neural is None:
            os.environ.pop("ENABLE_NEURAL_MEMORY", None)
        else:
            os.environ["ENABLE_NEURAL_MEMORY"] = prev_neural

    production = arms[_PRODUCTION_ARM]
    results: dict[str, Any] = {
        "run_date": run_date,
        "sudachi_version": _sudachi_version(),
        "corpus_version": corpus.meta.get("version"),
        "query_count": len(corpus.queries),
        "doc_count": len(corpus.documents),
        "recall_k": _RECALL_K,
        "production_arm": _PRODUCTION_ARM,
        # Top-level mirror of the production arm — keeps the pre-#967 results
        # shape so older readers and the live test's assertions stay valid.
        "overall": production["overall"],
        "per_bucket": production["per_bucket"],
        "source_recall@10": production["source_recall@10"],
        "arms": arms,
    }

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / f"{run_date}.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"eval-retrieval: wrote {out}")
    return results


def _main() -> int:
    import asyncio

    results = asyncio.run(run_retrieval_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
