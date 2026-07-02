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
# Per-doc retries for the embedding/upsert step. The Qdrant upsert can hit a
# transient client-side stall on local dev stacks (observed on WSL2: the
# request times out at the client's 5s default without ever reaching the
# server, at a random doc, while the server handles every arriving request in
# ~3ms). One reset-and-retry recovers it; a doc that fails repeatedly still
# aborts the run.
_INGEST_RETRIES = 2
# Max in-flight embedder/upsert drives during corpus ingest. Caps concurrency so
# we overlap per-doc embed latency instead of serializing it, without stampeding
# the embedding provider. Total ingest time ~ ceil(N / this) batches.
_INGEST_CONCURRENCY = 8
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
    """Ingest every corpus doc as a memory, then drive + await indexing for the
    WHOLE corpus at once.

    The caller owns ``id_map``; we populate it FULLY in the remember phase below
    BEFORE any indexing await, so a failure during indexing still lets teardown
    clean up every row created — no leaked rows on a partial ingest.

    ``remember()`` schedules embedding + Qdrant upsert as a fire-and-forget
    ``asyncio.create_task`` and returns before indexing completes. The previous
    implementation drove + polled each doc to a terminal state *before*
    remembering the next, which serialized the whole corpus behind a per-doc poll
    (up to ``attempts*interval_s`` each) and stopped the embedder from ever
    overlapping work. We instead remember everything first, then drive the
    pending embeddings concurrently and poll the whole set in ONE barrier — so
    indexing overlaps and total time scales with ``ceil(N / concurrency)`` rather
    than N. The "ingested AND indexed" guarantee (never recall a half-indexed
    corpus) is unchanged.
    """
    from models.schemas import RememberRequest

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
        # remember() has COMMITTED the Postgres row; record the id BEFORE driving
        # indexing so teardown's forget() can clean it up even if indexing raises.
        id_map[str(resp.memory_id)] = doc.id

    await _index_corpus(svc, [UUID(m) for m in id_map])


async def _index_corpus(svc: Any, memory_ids: list[UUID]) -> None:
    """Drive embedding/Qdrant indexing for every memory, then block until the
    whole set is searchable — with a bounded reset-and-retry for transient
    per-doc stalls (``_INGEST_RETRIES``).

    Turns the async, fire-and-forget writes into a single "ingested AND indexed"
    barrier for the corpus, so the eval never recalls against a half-indexed set.
    """
    pending = list(memory_ids)
    for attempt in range(_INGEST_RETRIES + 1):
        await _drive_pending_embeddings(svc, pending)
        # Generous ceiling: a safety net against a permanently stuck doc, not the
        # expected wait. The poll returns as soon as every doc is terminal.
        failed = await _await_all_indexed(svc, pending, timeout_s=max(120.0, 3.0 * len(pending)))
        if not failed:
            return
        if attempt == _INGEST_RETRIES:
            sample = "; ".join(f"{mid}: {err}" for mid, err in list(failed.items())[:3])
            raise RuntimeError(
                f"embedding FAILED for {len(failed)} doc(s) after "
                f"{_INGEST_RETRIES + 1} attempts ({sample}) — those docs are not in "
                "Qdrant, so eval would run against a half-indexed corpus"
            )
        print(
            f"eval-retrieval: {len(failed)} transient indexing failure(s) "
            f"(attempt {attempt + 1}/{_INGEST_RETRIES + 1}) — resetting & retrying"
        )
        await _reset_pending(svc, list(failed))
        pending = list(failed)


async def _drive_pending_embeddings(
    svc: Any, memory_ids: list[UUID], *, concurrency: int = _INGEST_CONCURRENCY
) -> None:
    """Drive ``process_pending_embedding`` for every id with bounded concurrency.

    ``process_pending_embedding`` opens its OWN session (``get_db()``) and claims
    atomically, so concurrent drives are safe: only one task processes each
    memory, and a doc already claimed by ``remember()``'s background task is a
    no-op. The semaphore caps in-flight embedder calls so we don't stampede the
    provider.
    """
    import asyncio

    from services.memory_service import process_pending_embedding

    sem = asyncio.Semaphore(concurrency)

    async def _drive(mid: UUID) -> None:
        async with sem:
            await process_pending_embedding(mid)

    await asyncio.gather(*(_drive(mid) for mid in memory_ids))


async def _reset_pending(svc: Any, memory_ids: list[UUID]) -> None:
    """Reset failed/stuck embeddings to ``pending`` so a retry can re-claim them.

    ``process_pending_embedding``'s claim UPDATE only matches ``pending`` or
    stale (>60s) ``processing`` rows — a ``failed`` row is terminal without this
    reset. #979: also reset the auto-retry budget (parity with the admin retry
    endpoint) so a doc that exhausted MAX_EMBEDDING_RETRIES during ingest can be
    driven again by the harness instead of staying stuck.
    """
    if not memory_ids:
        return
    from sqlalchemy import update

    from models.memory import Memory

    await svc.db.execute(
        update(Memory)
        .where(Memory.id.in_(memory_ids))
        .values(embedding_status="pending", embedding_error=None, embedding_retry_count=0)
    )
    await svc.db.commit()


async def _await_all_indexed(
    svc: Any, memory_ids: list[UUID], *, timeout_s: float = 120.0, interval_s: float = 0.1
) -> dict[UUID, str | None]:
    """Block until every id reaches a terminal embedding state, polling the WHOLE
    set in one query per tick.

    Returns a map of id → error for the ones that ended ``failed`` (empty when
    all succeeded). A ``failed`` row is collected, NOT raised, so the caller can
    reset-and-retry the failures without losing the docs that already succeeded.
    A timeout (some id never terminal) raises, so the eval never proceeds against
    a half-indexed corpus.

    The single-query barrier replaces the old per-doc poll: total wait is bounded
    by the slowest *concurrent* batch, not the sum of N per-doc polls. The SELECT
    is column-only, so it reads committed state past the session identity map
    under READ COMMITTED — status is committed by ``process_pending_embedding``'s
    own session.
    """
    import asyncio

    from sqlalchemy import select

    from models.memory import Memory

    remaining: set[UUID] = set(memory_ids)
    failed: dict[UUID, str | None] = {}
    for _ in range(max(1, int(timeout_s / interval_s))):
        rows = (
            await svc.db.execute(
                select(Memory.id, Memory.embedding_status, Memory.embedding_error).where(
                    Memory.id.in_(remaining)
                )
            )
        ).all()
        still: set[UUID] = set()
        for mid, status, err in rows:
            if status == "success":
                continue
            if status == "failed":
                failed[mid] = err
            else:
                still.add(mid)
        remaining = still
        if not remaining:
            return failed
        await asyncio.sleep(interval_s)

    raise RuntimeError(
        f"{len(remaining)} embedding(s) never reached a terminal state within "
        f"{timeout_s:.0f}s — eval would run against a half-indexed corpus"
    )


async def run_retrieval_eval(write: bool = True, run_date: str | None = None) -> dict[str, Any]:
    """Ingest the corpus, run every query, compute metrics, write results JSON.

    Args:
        write: when True, persist results/<run_date>.json.
        run_date: YYYY-MM-DD label for the results file; defaults to today (UTC).

    Returns the results dict (also written to disk when ``write``).
    """
    from auth.workspace_roles import WorkspaceRole
    from config.constants import EMBEDDING_MODEL_REGISTRY
    from config.settings import get_settings
    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name
    from models.auth import Context, Workspace, WorkspaceMember
    from models.config import ContextSearchConfig
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
        # The raw ORM Context above bypasses ContextService.create_context, which
        # is where the per-context embedding config is stamped from settings and
        # the model-specific Qdrant collection is ensured. Without an explicit
        # ContextSearchConfig row, the lazy create_or_get defaults to
        # text-embedding-3-small/512 and ingest routes to a collection that does
        # not exist on non-default embedding stacks (e.g. a local
        # qwen3-embedding:0.6b/1024 rig) — stamp the config and ensure the
        # collection exactly like the production create path.
        settings = get_settings()
        emb_model = settings.embedding_model
        emb_dims = EMBEDDING_MODEL_REGISTRY.get(emb_model, (settings.embedding_dimensions, ""))[0]
        # Mirror ContextService.create_context's defaults (not just the
        # embedding fields) so an eval context behaves like a production one if
        # any reranking / weighting default is ever consulted.
        db.add(
            ContextSearchConfig(
                context_id=ctx.id,
                semantic_weight=0.6,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2-lite",
                embedding_model=emb_model,
                embedding_dimensions=emb_dims,
            )
        )
        await db.flush()
        await db.commit()

        svc = MemoryService(db)
        # id_map is owned here and populated incrementally by _ingest_corpus, so
        # the finally below ALWAYS cleans up whatever was created — even if
        # ingest itself raises partway through (no leaked workspace/context/rows).
        # ensure_kagura_memories_collection is INSIDE the try so that if it
        # raises (e.g. Qdrant unavailable), _teardown still removes the
        # already-committed workspace/context/config rows instead of leaking them.
        id_map: dict[str, str] = {}
        try:
            await ensure_kagura_memories_collection(
                emb_dims, get_collection_name(emb_model, emb_dims)
            )
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
