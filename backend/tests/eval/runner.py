"""Live retrieval-eval orchestration (Issue #344).

Separated from ``test_retrieval_quality.py`` so it can be driven either by the
skip-guarded pytest test or directly via ``python -m tests.eval.runner`` (the
``make eval-retrieval`` target). All heavy imports (DB, services) are local to
the functions so importing this module is cheap and CI-safe.

Produces ``results/<YYYY-MM-DD>.json`` from a REAL run only — never fabricated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tests.eval.metrics import (
    mean_precision_at_k,
    mrr_at_k,
    source_recall_share,
)
from tests.eval.tools.corpus import BUCKETS, Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_RECALL_K = 10
_P_AT = (5, 10)
_MRR_AT = 10


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
    AWAIT ``process_pending_embedding`` for each memory here — turning the async
    write into a synchronous "ingested AND indexed" barrier.
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
        # Record BEFORE awaiting indexing so a failure in
        # process_pending_embedding still leaves the id in the caller's map for
        # teardown (the PG/Qdrant write from remember() already happened).
        id_map[str(resp.memory_id)] = doc.id
        # Deterministically drive the embedding/upsert to completion before any
        # recall runs (idempotent with the background task remember() fired).
        await process_pending_embedding(resp.memory_id)


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
            # forget() deletes from BOTH Postgres and Qdrant.
            await svc.forget(ForgetRequest(memory_id=UUID(mid)), owner, ctx_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
    try:
        await db.execute(_delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id))
        await db.execute(_delete(Context).where(Context.id == ctx_id))
        await db.execute(_delete(Workspace).where(Workspace.id == ws_id))
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        await db.rollback()


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
    """Recall every query, compute metrics, optionally write results JSON."""
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
            request=RecallRequest(query=q.text, k=_RECALL_K, search_mode="hybrid"),
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

    def _bucket_metrics(rankings: list[tuple[list[str], set[str]]]) -> dict[str, Any]:
        return {
            "n": len(rankings),
            **{f"p@{k}": round(mean_precision_at_k(rankings, k), 4) for k in _P_AT},
            f"mrr@{_MRR_AT}": round(mrr_at_k(rankings, _MRR_AT), 4),
        }

    results: dict[str, Any] = {
        "run_date": run_date,
        "sudachi_version": _sudachi_version(),
        "corpus_version": corpus.meta.get("version"),
        "query_count": len(corpus.queries),
        "doc_count": len(corpus.documents),
        "recall_k": _RECALL_K,
        "overall": _bucket_metrics(all_rankings),
        "per_bucket": {b: _bucket_metrics(per_bucket_rankings[b]) for b in BUCKETS},
        "source_recall@10": {
            k: round(v, 4)
            for k, v in source_recall_share(
                all_retrieved_sources, len(all_retrieved_sources)
            ).items()
        },
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
