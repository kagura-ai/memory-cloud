"""Live H4 update-correctness orchestration (Day-5, prereg-v1 F3).

Companion to ``tests.eval.runner`` (#967) and ``tests.eval.replay_runner``
(#969): those measure static retrieval quality and usage-driven compounding;
this one measures whether memory-cloud's update/consolidation machinery
prefers the CURRENT fact over a superseded STALE fact once both have been
independently remembered — the estimand behind prereg-v1 H4.

Per the Day-5 design (docs/plans/2026-07-03-day5-update-correctness.md in the
eval repo), memory-cloud has no supersedes/contradicts edge between two
independently-remembered documents; the only levers that can prefer the
current fact are Reinforce cold-start recency and Sleep full ``dedup_merge``
(judge-LLM on). Isolating those from the rest of the system requires two
throwaway contexts that differ ONLY in update machinery:

- **vanilla_rag**: no Sleep (``sleep_mode="skip"``), ``reinforce_enabled=False``,
  neural off, ``search_mode="semantic"`` — the must-clear baseline.
- **mc_update** (gated): ``sleep_mode="full"`` + one Sleep full pass (judge-LLM
  as configured) + ``reinforce_enabled=True``, same ``search_mode="semantic"``
  so the CONTRAST isolates update machinery, not retrieval mode.
- **mc_prod** (supporting, not gated): the SAME mc context scored again with
  ``search_mode="hybrid"`` + neural memory on (production posture) — scored
  LAST because ``ENABLE_NEURAL_MEMORY=true`` performs Hebbian graph WRITES on
  every ``recall()`` call, which must not contaminate the gated mc_update
  scoring pass.

A longitudinal update-slice corpus is ingested (v1-stale docs, fillers, then
v2-current docs, in that exact list order) into BOTH contexts identically;
every stale (``-v1``) memory row is then backdated 30 days (D3) so the
recency-based levers above are not inert-by-construction (a same-second
ingest gives every recency mechanism zero differential to work with).

Produces ``results/<label>-<date>.json`` from a REAL run only — never
fabricated. Heavy imports (DB, services) are function-local so importing this
module is cheap and CI-safe (same convention as ``runner.py``).
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tests.eval.runner import _ingest_corpus, _results_filename
from tests.eval.tools.corpus import Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Pre-declared constants (prereg-v1 H4 / Day-5 design doc D2-D5) — bake in,
# do not change without a corresponding prereg amendment.
K = 10
BACKDATE_DAYS = 30
SEED = 20260703
N_RESAMPLES = 10_000
ALPHA = 0.05
DELTA_UPDATE = 0.15
MIN_COMPLETE_PAIRS = 25

# Arm names. vanilla_rag / mc_update are the gated H4 contrast (D1); mc_prod
# is supporting only. Order below (as used by ``_score_and_write``) is
# load-bearing for the same reason as ``runner.py``'s ``_ARMS``: mc_prod's
# neural-on recall() performs Hebbian graph writes and must run LAST so it
# cannot warm the graph for mc_update's (gated) scoring pass.
ARM_VANILLA_RAG = "vanilla_rag"
ARM_MC_UPDATE = "mc_update"
ARM_MC_PROD = "mc_prod"

# The five possible per-query outcomes of ``classify_update_outcome``.
OUTCOME_CURRENT_OVER_STALE = "current_over_stale"
OUTCOME_STALE_OVER_CURRENT = "stale_over_current"
OUTCOME_CURRENT_ONLY = "current_only"
OUTCOME_STALE_ONLY = "stale_only"
OUTCOME_NEITHER = "neither"


def classify_update_outcome(
    ranked_doc_ids: Sequence[str], current: str, stale: str, k: int
) -> dict[str, Any]:
    """Classify one query's top-``k`` ranking against its update pair.

    ``current_rank`` / ``stale_rank`` are 1-based positions of ``current`` /
    ``stale`` within ``ranked_doc_ids[:k]`` (``None`` if absent from that
    window — a doc at position ``k+1`` or later counts as absent, matching
    "both retrievable in top-k" from the prereg).

    Outcomes:
        - ``current_over_stale`` / ``stale_over_current``: both present,
          ordered by rank (lower rank = higher in the results).
        - ``current_only`` / ``stale_only``: exactly one present (dedup
          REMOVAL of the stale doc lands here — "update-by-removal", D4).
        - ``neither``: neither doc appears in the top-``k`` window.

    Raises:
        ValueError: ``current == stale`` — a query cannot be evaluated
            against itself; this is a corpus/caller bug, not a scorable case.
    """
    if current == stale:
        raise ValueError(
            f"classify_update_outcome: current and stale doc ids are identical "
            f"({current!r}) — a query's update pair must name two distinct docs"
        )

    window = ranked_doc_ids[:k]
    current_rank = next((i + 1 for i, d in enumerate(window) if d == current), None)
    stale_rank = next((i + 1 for i, d in enumerate(window) if d == stale), None)

    if current_rank is not None and stale_rank is not None:
        outcome = (
            OUTCOME_CURRENT_OVER_STALE if current_rank < stale_rank else OUTCOME_STALE_OVER_CURRENT
        )
    elif current_rank is not None:
        outcome = OUTCOME_CURRENT_ONLY
    elif stale_rank is not None:
        outcome = OUTCOME_STALE_ONLY
    else:
        outcome = OUTCOME_NEITHER

    return {"outcome": outcome, "current_rank": current_rank, "stale_rank": stale_rank}


async def _backdate_v1(db: Any, id_map: dict[str, str], days: int) -> None:
    """Backdate every ``-v1`` (stale) memory's ``created_at``/``updated_at`` by
    ``days`` (D3 — simulates longitudinal time passage between doc versions).

    A no-op (no query, no commit) when ``id_map`` carries no ``-v1`` doc —
    keeps this safe to call for a corpus slice that happens to have none.
    """
    from sqlalchemy import update

    from models.memory import Memory

    v1_ids = [UUID(mid) for mid, doc_id in id_map.items() if doc_id.endswith("-v1")]
    if not v1_ids:
        return
    delta = timedelta(days=days)
    await db.execute(
        update(Memory)
        .where(Memory.id.in_(v1_ids))
        .values(created_at=Memory.created_at - delta, updated_at=Memory.updated_at - delta)
    )
    await db.commit()


async def _run_mc_sleep(db: Any, owner: str, ws_id: Any, ctx_id: Any) -> dict[str, Any]:
    """One Sleep full pass over the MC context, judge-LLM AS CONFIGURED.

    Unlike ``replay_runner._run_sleep``, the judge is NOT blanked
    (``sleep_llm_provider=""``): the whole point of the MC arm is the real
    dedup_merge judge (``SLEEP_LLM_PROVIDER=self_hosted``, qwen3.5-9b),
    per D1. A failed Sleep run is recorded, not raised — the MC arm is still
    scored (a failed consolidation is itself part of the honest result), and
    ``_summarize_sleep_report`` never raises for an unexpected report shape.
    """
    from neural.config import NeuralMemoryConfig
    from services.sleep.orchestrator import SleepOrchestrator

    NeuralMemoryConfig.invalidate_cache()
    config = await NeuralMemoryConfig.from_db(db)
    try:
        await SleepOrchestrator(db).run(
            owner, workspace_id=str(ws_id), context_id=str(ctx_id), config=config
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — recorded in results, not fatal
        await db.rollback()
        return {"ran": True, "ok": False, "error": str(exc)}

    return await _summarize_sleep_report(db, owner)


# Best-effort scalar/JSON fields lifted from the latest SleepReport into the
# results JSON. Kept as a module constant so ``_summarize_sleep_report``'s
# per-field try/except loop is easy to audit against ``models.sleep.SleepReport``.
_SLEEP_REPORT_FIELDS: tuple[str, ...] = (
    "status",
    "llm_call_failures",  # #1183: first-class judge-failure count
    "memories_processed",
    "edges_created",
    "memories_merged",
    "memories_promoted",
    "memories_flagged",
    "error_message",
    "edge_discovery_result",
    "dedup_result",
    "importance_result",
    "consolidation_result",
    "reindex_result",
)


async def _summarize_sleep_report(db: Any, owner: str) -> dict[str, Any]:
    """Best-effort JSON-safe summary of the latest ``SleepReport`` for ``owner``.

    Never raises: a lookup failure or an unexpected report shape degrades to
    a partial/error summary dict rather than aborting the whole eval run (at
    minimum, the caller always learns that Sleep ran and whether it
    completed).
    """
    from sqlalchemy import select

    from models.sleep import SleepReport

    summary: dict[str, Any] = {"ran": True, "ok": False}
    try:
        report = (
            await db.execute(
                select(SleepReport)
                .where(SleepReport.user_id == owner)
                .order_by(SleepReport.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup/introspection
        summary["error"] = f"sleep report lookup failed: {exc}"
        return summary

    if report is None:
        summary["error"] = "sleep run left no report"
        return summary

    for field_name in _SLEEP_REPORT_FIELDS:
        try:
            summary[field_name] = getattr(report, field_name)
        except Exception:  # noqa: BLE001 — best-effort field introspection
            continue
    summary["ok"] = summary.get("status") == "completed"
    # Roll judge-LLM failures up to the top level: "completed" phases can hide
    # a fully non-functional judge (every call failed) inside per-phase detail
    # dicts, which nearly buried the Day-5 run-0 provider misconfiguration.
    failures = 0
    for phase_field in ("edge_discovery_result", "dedup_result", "importance_result"):
        phase = summary.get(phase_field)
        if isinstance(phase, dict):
            details = phase.get("details") if isinstance(phase.get("details"), dict) else phase
            for key in ("llm_call_failures", "llm_failures"):
                val = details.get(key)
                if isinstance(val, int):
                    failures += val
    summary["llm_call_failures_total"] = failures
    return summary


async def _score_update_arm(
    svc: Any,
    queries: Sequence[Any],
    id_map: dict[str, str],
    owner: str,
    ctx_id: Any,
    ws_id: Any,
    search_mode: str,
) -> dict[str, Any]:
    """Recall every update query with ``search_mode`` and classify the outcome."""
    from models.schemas import RecallRequest

    per_query: list[dict[str, Any]] = []
    for q in queries:
        resp = await svc.recall(
            request=RecallRequest(query=q.text, k=K, search_mode=search_mode),
            user_id=owner,
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        ranked_doc_ids = [id_map.get(str(r.memory_id), "?") for r in resp.results]
        classification = classify_update_outcome(
            ranked_doc_ids, q.update["current"], q.update["stale"], K
        )
        per_query.append({"query_id": q.id, **classification})
    return {"per_query": per_query}


async def _teardown_both(
    svc: Any,
    db: Any,
    owner: str,
    ws_id: Any,
    ctx_ids: Sequence[Any],
    memory_id_lists: Sequence[list[str]],
) -> None:
    """Remove throwaway eval data for BOTH contexts + the shared workspace.

    Deliberately NOT a call to ``runner._teardown`` once per context: that
    function's tail unconditionally deletes the Workspace row, and
    ``Context.workspace_id`` / ``Memory.context_id`` both ``ON DELETE
    CASCADE``. Calling it for ctx A first would cascade-delete ctx B's
    (Postgres) Memory rows via the Workspace delete BEFORE ctx B's own
    forget() loop ran — forget() would then find nothing to delete and the
    Qdrant points for ctx B's memories would leak silently. Instead: forget()
    every memory in EVERY context first (best-effort, rolling back a poisoned
    transaction so the rest of the loop still runs — same discipline as
    ``runner._teardown``), then do ONE combined Context+Workspace deletion.
    """
    from sqlalchemy import delete as _delete

    from models.auth import Context, Workspace, WorkspaceMember
    from models.schemas import ForgetRequest

    for ctx_id, memory_ids in zip(ctx_ids, memory_id_lists, strict=True):
        for mid in memory_ids:
            try:
                await svc.forget(ForgetRequest(memory_id=UUID(mid)), owner, ctx_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                await db.rollback()
    try:
        await db.execute(_delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id))
        await db.execute(_delete(Context).where(Context.id.in_(list(ctx_ids))))
        await db.execute(_delete(Workspace).where(Workspace.id == ws_id))
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        await db.rollback()


async def _score_and_write(
    svc: Any,
    corpus: Corpus,
    update_queries: Sequence[Any],
    id_map_vr: dict[str, str],
    id_map_mc: dict[str, str],
    owner: str,
    ws_id: Any,
    ctx_vr_id: Any,
    ctx_mc_id: Any,
    *,
    run_date: str,
    write: bool,
    label: str,
    corpus_path: str,
    embedding_model: str,
    embedding_dimensions: int,
    sleep_summary: dict[str, Any],
) -> dict[str, Any]:
    """Score all three arms (order load-bearing, see module docstring),
    assemble the results dict, and optionally write it to disk."""
    arms: dict[str, Any] = {}
    prev_neural = os.environ.get("ENABLE_NEURAL_MEMORY")
    try:
        os.environ["ENABLE_NEURAL_MEMORY"] = "false"
        arms[ARM_VANILLA_RAG] = await _score_update_arm(
            svc, update_queries, id_map_vr, owner, ctx_vr_id, ws_id, "semantic"
        )
        arms[ARM_MC_UPDATE] = await _score_update_arm(
            svc, update_queries, id_map_mc, owner, ctx_mc_id, ws_id, "semantic"
        )
        os.environ["ENABLE_NEURAL_MEMORY"] = "true"
        arms[ARM_MC_PROD] = await _score_update_arm(
            svc, update_queries, id_map_mc, owner, ctx_mc_id, ws_id, "hybrid"
        )
    finally:
        if prev_neural is None:
            os.environ.pop("ENABLE_NEURAL_MEMORY", None)
        else:
            os.environ["ENABLE_NEURAL_MEMORY"] = prev_neural

    results: dict[str, Any] = {
        "run_date": run_date,
        "experiment": "day5-update-correctness",
        "label": label,
        "corpus_path": corpus_path,
        "content_sha256": corpus.meta.get("content_sha256"),
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "k": K,
        "backdate_days": BACKDATE_DAYS,
        "judge_model": os.environ.get("SLEEP_LLM_MODEL", ""),
        "sleep_summary": sleep_summary,
        "arms": arms,
    }

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / _results_filename(label, run_date)
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"update-runner: wrote {out}")
    return results


async def run_update_eval(
    corpus_path: str,
    label: str,
    write: bool = True,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Run the full H4 two-arm (three-scoring) update-correctness experiment.

    Args:
        corpus_path: path to the update-slice corpus YAML
            (``fixtures/update_slice.yaml``).
        label: results-filename prefix (see ``tests.eval.runner._results_filename``).
        write: when True, persist ``results/<label>-<run_date>.json``.
        run_date: YYYY-MM-DD label for the results file; defaults to today (UTC).

    Returns the results dict (also written to disk when ``write``).

    Raises:
        RuntimeError: the corpus has no queries carrying ``Query.update`` —
            nothing to score for H4 (checked before any DB access).
    """
    corpus = load_corpus(Path(corpus_path))
    update_queries = [q for q in corpus.queries if q.update is not None]
    if not update_queries:
        raise RuntimeError(
            f"corpus {corpus_path!r} has no update queries (Query.update) — nothing to score for H4"
        )

    from auth.workspace_roles import WorkspaceRole
    from config.constants import EMBEDDING_MODEL_REGISTRY
    from config.settings import get_settings
    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name
    from models.auth import Context, Workspace, WorkspaceMember
    from models.config import ContextSearchConfig
    from services.memory_service import MemoryService
    from utils.datetime import utcnow

    run_date = run_date or utcnow().strftime("%Y-%m-%d")

    settings = get_settings()
    emb_model = settings.embedding_model
    emb_dims = EMBEDDING_MODEL_REGISTRY.get(emb_model, (settings.embedding_dimensions, ""))[0]

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
        # ctx_vr: exactly runner.py's stamp, sleep_mode left at its default
        # ("skip") and reinforce_enabled pinned False EXPLICITLY — the Vanilla
        # RAG baseline. #1207 flipped the column default to true, so the VR
        # arm can no longer rely on the model default staying off.
        ctx_vr = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"eval-ctx-vr-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        # ctx_mc: same stamp, but sleep_mode="full" (D1) — one Sleep pass runs
        # against it below, and its ContextSearchConfig turns reinforce on.
        ctx_mc = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"eval-ctx-mc-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
            sleep_mode="full",
        )
        db.add(ws)
        await db.flush()
        db.add(ctx_vr)
        db.add(ctx_mc)
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        db.add(
            ContextSearchConfig(
                context_id=ctx_vr.id,
                semantic_weight=0.6,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="self_hosted",
                reranker_model="qwen3-reranker-4b",
                embedding_model=emb_model,
                embedding_dimensions=emb_dims,
                reinforce_enabled=False,
            )
        )
        db.add(
            ContextSearchConfig(
                context_id=ctx_mc.id,
                semantic_weight=0.6,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="self_hosted",
                reranker_model="qwen3-reranker-4b",
                embedding_model=emb_model,
                embedding_dimensions=emb_dims,
                reinforce_enabled=True,
            )
        )
        await db.flush()
        await db.commit()

        svc = MemoryService(db)
        # Both id_maps are owned here and populated incrementally by
        # _ingest_corpus, so the finally below ALWAYS cleans up whatever was
        # created in EITHER context — even on a partial-ingest raise.
        id_map_vr: dict[str, str] = {}
        id_map_mc: dict[str, str] = {}
        try:
            await ensure_kagura_memories_collection(
                emb_dims, get_collection_name(emb_model, emb_dims)
            )
            await _ingest_corpus(svc, corpus, owner, ctx_vr.id, ws.id, id_map_vr)
            await _ingest_corpus(svc, corpus, owner, ctx_mc.id, ws.id, id_map_mc)

            # D3: backdate every stale (-v1) memory 30 days in BOTH contexts,
            # identically, before any scoring — simulates the time passage the
            # update estimand presupposes.
            await _backdate_v1(db, id_map_vr, BACKDATE_DAYS)
            await _backdate_v1(db, id_map_mc, BACKDATE_DAYS)

            # MC only: one Sleep full pass (judge-LLM as configured).
            sleep_summary = await _run_mc_sleep(db, owner, ws.id, ctx_mc.id)

            return await _score_and_write(
                svc,
                corpus,
                update_queries,
                id_map_vr,
                id_map_mc,
                owner,
                ws.id,
                ctx_vr.id,
                ctx_mc.id,
                run_date=run_date,
                write=write,
                label=label,
                corpus_path=corpus_path,
                embedding_model=emb_model,
                embedding_dimensions=emb_dims,
                sleep_summary=sleep_summary,
            )
        finally:
            await _teardown_both(
                svc,
                db,
                owner,
                ws.id,
                [ctx_vr.id, ctx_mc.id],
                [list(id_map_vr), list(id_map_mc)],
            )

    raise RuntimeError("database session unavailable — is the stack up?")


def _main() -> int:
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--corpus",
        dest="corpus_path",
        required=True,
        help="update-slice corpus YAML path (e.g. fixtures/update_slice.yaml)",
    )
    ap.add_argument(
        "--label",
        required=True,
        help="results filename prefix, e.g. day5-update-run0",
    )
    ap.add_argument(
        "--no-write", action="store_true", help="print only, skip the results JSON artifact"
    )
    args = ap.parse_args()

    results = asyncio.run(
        run_update_eval(corpus_path=args.corpus_path, label=args.label, write=not args.no_write)
    )
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
