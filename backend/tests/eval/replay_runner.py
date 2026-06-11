"""Live cold→replay→warm compounding experiment orchestration (Issue #969).

Tier B companion to ``tests.eval.runner`` (#967): the static harness measures
retrieval quality on a frozen corpus; this one measures whether quality
*compounds with use*. Protocol per replay mode (each in its own throwaway
workspace, corpus held fixed throughout):

    ingest → COLD checkpoint → replay co-recall traffic (neural ON, N rounds)
           → WARM_REPLAY checkpoint → Sleep ``edges_only`` consolidation
           → WARM_SLEEP checkpoint → per-lane lift tables

Each checkpoint measures two lanes (see ``tests.eval.compounding`` for the
Issue #120 design facts this rests on):

- **graph lane** (primary): companion recovery on the held-out multi-gold
  probes via ``explore()`` activation spreading from the probe's seed gold
  doc — the surface that reads the learned layer.
- **recall lane** (control): hybrid ``recall()`` P@5/MRR/nDCG over all corpus
  queries, measured with neural memory disabled so the measurement itself is
  read-only w.r.t. the graph. Flat-by-design (recall ranking is
  usage-independent); a moving number here is a regression signal.

Checkpoints additionally snapshot the per-origin edge stats of the eval
context, so the lift is attributable: Hebbian edges come from replay,
``semantic`` edges from the Sleep run.

Produces ``results/compounding-<YYYY-MM-DD>.json`` from a REAL run only —
never fabricated. Heavy imports are function-local so importing this module
is cheap and CI-safe (same convention as ``runner.py``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tests.eval.compounding import (
    REPLAY_MODES,
    PairAudit,
    ProbeSpec,
    ReplayPlan,
    build_replay_plan,
    classify_pair,
    compute_lift,
    recall_at_k,
    summarize_gate_audit,
)
from tests.eval.metrics import mrr_at_k
from tests.eval.runner import _ingest_corpus, _score_arm, _sudachi_version, _teardown
from tests.eval.tools.corpus import Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_RECALL_K = 10
_REPLAY_ROUNDS = 8
_EXPLORE_DEPTH = 2
# ExploreRequest's default (0.05). After 8 replay rounds Hebbian weights for
# genuinely co-recalled pairs sit well above this; pairs that never co-recall
# stay below it, so the default separates signal from floor noise.
_EXPLORE_MIN_WEIGHT = 0.05
_RECOVERY_AT = (5, 10)
_MRR_AT = 10


async def run_compounding_eval(
    write: bool = True,
    run_date: str | None = None,
    rounds: int = _REPLAY_ROUNDS,
    modes: tuple[str, ...] = REPLAY_MODES,
) -> dict[str, Any]:
    """Run the full cold→replay→warm experiment for every replay mode.

    Args:
        write: when True, persist ``results/compounding-<run_date>.json``.
        run_date: YYYY-MM-DD label for the results file; defaults to today (UTC).
        rounds: replay repetitions of the workload (per mode).
        modes: replay modes to run, each in an isolated workspace.

    Returns the results dict (also written to disk when ``write``).
    """
    from utils.datetime import utcnow

    corpus = load_corpus()
    run_date = run_date or utcnow().strftime("%Y-%m-%d")

    mode_blocks: dict[str, Any] = {}
    for mode in modes:
        plan = build_replay_plan(corpus, mode=mode, rounds=rounds)
        mode_blocks[mode] = await _run_mode(corpus, plan)

    results: dict[str, Any] = {
        "run_date": run_date,
        "experiment": "compounding",
        "issue": 969,
        "sudachi_version": _sudachi_version(),
        "corpus_version": corpus.meta.get("version"),
        "query_count": len(corpus.queries),
        "doc_count": len(corpus.documents),
        "recall_k": _RECALL_K,
        "rounds": rounds,
        "explore_depth": _EXPLORE_DEPTH,
        "explore_min_weight": _EXPLORE_MIN_WEIGHT,
        "design_note": (
            "Per Issue #120 the neural graph is read by explore(), not by "
            "recall() ranking — so compounding is measured on the graph lane "
            "(companion recovery from a seed gold doc) while the recall lane "
            "is the flat-by-design stability control. Corpus is held fixed; "
            "publish lift deltas only, never absolute numbers."
        ),
        "modes": mode_blocks,
    }

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / f"compounding-{run_date}.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"eval-compounding: wrote {out}")
    return results


async def _run_mode(corpus: Corpus, plan: ReplayPlan) -> dict[str, Any]:
    """Provision an isolated workspace, run one mode's experiment, tear down."""
    from auth.workspace_roles import WorkspaceRole
    from db.base import get_db
    from models.auth import Context, Workspace, WorkspaceMember
    from services.memory_service import MemoryService

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
            # The warm_sleep checkpoint needs the consolidation pass to run;
            # the orchestrator skips contexts left on the default "skip".
            # "edges_only" runs exactly the edge-discovery phase — the one
            # that writes to the learned layer under measurement.
            sleep_mode="edges_only",
        )
        db.add(ws)
        await db.flush()
        db.add(ctx)
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        await db.flush()
        await db.commit()

        svc = MemoryService(db)
        id_map: dict[str, str] = {}
        try:
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)
            return await _measure_replay_measure(svc, db, corpus, plan, id_map, owner, ctx, ws)
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))

    raise RuntimeError("database session unavailable — is the stack up?")


async def _measure_replay_measure(
    svc: Any,
    db: Any,
    corpus: Corpus,
    plan: ReplayPlan,
    id_map: dict[str, str],
    owner: str,
    ctx: Any,
    ws: Any,
) -> dict[str, Any]:
    """Drive the cold → replay → warm_replay → sleep → warm_sleep sequence."""
    docs_by_id = corpus.docs_by_id
    seed_mem_by_doc = {doc_id: mem_id for mem_id, doc_id in id_map.items()}

    checkpoints: dict[str, dict[str, Any]] = {}
    checkpoints["cold"] = await _checkpoint(
        svc, db, corpus, plan, docs_by_id, id_map, seed_mem_by_doc, owner, ctx, ws
    )

    # #982 demonstration: calibrate the edge_gate threshold from THIS corpus's
    # non-gold pairwise cosine distribution and seed a (transient, model-global)
    # edge_gate calibration row so the replay path's resolve_edge_threshold
    # returns the calibrated value instead of the absolute 0.5 — exactly the
    # production path. The row is deleted in the finally below; it is model-
    # global (v1 resolve ignores context_id) so it briefly affects any
    # concurrent recall on the same (model, dims). Acceptable for a local
    # verification run with guaranteed cleanup.
    # Seed INSIDE the try so its commit is always paired with the finally
    # cleanup — _seed_edge_calibration guarantees that once it commits the row
    # it returns seeded=True + model/dimensions even if its post-commit
    # diagnostic raises, so the finally can always delete the transient row.
    edge_calibration: dict[str, Any] = {"seeded": False}
    try:
        edge_calibration = await _seed_edge_calibration(db, plan, id_map, ctx)
        gate_audit = await _gate_audit(svc, db, corpus, plan, id_map, owner, ctx, ws)
        replay_recalls = await _replay(svc, corpus, plan, owner, ctx, ws)
        checkpoints["warm_replay"] = await _checkpoint(
            svc, db, corpus, plan, docs_by_id, id_map, seed_mem_by_doc, owner, ctx, ws
        )

        sleep_info = await _run_sleep(db, owner, ctx, ws)
        checkpoints["warm_sleep"] = await _checkpoint(
            svc, db, corpus, plan, docs_by_id, id_map, seed_mem_by_doc, owner, ctx, ws
        )
    finally:
        if edge_calibration.get("seeded"):
            await _delete_edge_calibration(
                db, edge_calibration["model"], edge_calibration["dimensions"]
            )

    lift = {
        lane: {
            "cold_to_warm_replay": compute_lift(
                checkpoints["cold"][lane], checkpoints["warm_replay"][lane]
            ),
            "warm_replay_to_warm_sleep": compute_lift(
                checkpoints["warm_replay"][lane], checkpoints["warm_sleep"][lane]
            ),
            "cold_to_warm_sleep": compute_lift(
                checkpoints["cold"][lane], checkpoints["warm_sleep"][lane]
            ),
        }
        for lane in ("graph_lane", "recall_lane")
    }

    return {
        "mode": plan.mode,
        "probe_count": len(plan.probes),
        "probe_query_ids": [p.query_id for p in plan.probes],
        "replay_query_count": len(plan.replay_query_ids),
        "replay_recalls": replay_recalls,
        "edge_calibration": edge_calibration,
        "gate_audit": gate_audit,
        "sleep": sleep_info,
        "checkpoints": checkpoints,
        "lift": lift,
    }


async def _checkpoint(
    svc: Any,
    db: Any,
    corpus: Corpus,
    plan: ReplayPlan,
    docs_by_id: dict[str, Any],
    id_map: dict[str, str],
    seed_mem_by_doc: dict[str, str],
    owner: str,
    ctx: Any,
    ws: Any,
) -> dict[str, Any]:
    """One measurement snapshot: graph lane + recall lane + edge stats.

    Every measurement here is read-only with respect to the neural graph:
    ``explore()`` only traverses edges, and the recall-lane arm runs with
    ``ENABLE_NEURAL_MEMORY=false`` so no Hebbian writes contaminate the
    checkpoint (per #967's published invariant, hybrid scores are identical
    with the flag on or off).
    """
    graph_lane = await _measure_graph_lane(
        svc, plan.probes, seed_mem_by_doc, id_map, owner, ctx, ws
    )

    prev_neural = os.environ.get("ENABLE_NEURAL_MEMORY")
    os.environ["ENABLE_NEURAL_MEMORY"] = "false"
    try:
        arm = await _score_arm(svc, corpus, docs_by_id, id_map, owner, ctx.id, ws.id, "hybrid")
    finally:
        _restore_env("ENABLE_NEURAL_MEMORY", prev_neural)

    return {
        "graph_lane": graph_lane,
        "recall_lane": arm["overall"],
        "edge_stats": await _edge_stats(db, ctx.id),
    }


async def _measure_graph_lane(
    svc: Any,
    probes: tuple[ProbeSpec, ...],
    seed_mem_by_doc: dict[str, str],
    id_map: dict[str, str],
    owner: str,
    ctx: Any,
    ws: Any,
) -> dict[str, Any]:
    """Companion recovery via activation spreading, per held-out probe.

    For each probe: seed at its first gold doc's memory, spread activation
    (``explore``), rank the surfaced docs by activation, and score how much
    of the remaining gold set (the companions) is recovered.
    """
    from models.schemas import ExploreRequest

    rankings: list[tuple[list[str], set[str]]] = []
    seeds_in_graph = 0
    for probe in probes:
        seed_mem_id = seed_mem_by_doc[probe.seed_doc]
        resp = await svc.explore(
            request=ExploreRequest(
                memory_id=UUID(seed_mem_id),
                depth=_EXPLORE_DEPTH,
                min_weight=_EXPLORE_MIN_WEIGHT,
            ),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
        )
        if resp.metadata.get("reason") != "seed_not_in_graph":
            seeds_in_graph += 1
        related = sorted(resp.related_memories, key=lambda m: m.activation, reverse=True)
        ranked = [id_map.get(str(m.memory_id), "?") for m in related]
        rankings.append((ranked, set(probe.companion_docs)))

    mean_recovery = {
        f"recovery@{k}": round(
            sum(recall_at_k(r, rel, k) for r, rel in rankings) / len(rankings), 4
        )
        for k in _RECOVERY_AT
    }
    return {
        "n": len(rankings),
        **mean_recovery,
        f"mrr@{_MRR_AT}": round(mrr_at_k(rankings, _MRR_AT), 4),
        "seeds_in_graph": seeds_in_graph,
    }


async def _replay(svc: Any, corpus: Corpus, plan: ReplayPlan, owner: str, ctx: Any, ws: Any) -> int:
    """Drive the replay workload with neural memory ON (the warming phase).

    Each ``recall()`` co-activates its top results and applies Hebbian edge
    updates — this is the production write path, not a simulation. The
    workload is deterministic: the plan's query order, repeated ``rounds``
    times. Returns the number of recalls issued.
    """
    from models.schemas import RecallRequest

    queries_by_id = {q.id: q for q in corpus.queries}
    prev_neural = os.environ.get("ENABLE_NEURAL_MEMORY")
    os.environ["ENABLE_NEURAL_MEMORY"] = "true"
    recalls = 0
    try:
        for _ in range(plan.rounds):
            for query_id in plan.replay_query_ids:
                await svc.recall(
                    request=RecallRequest(
                        query=queries_by_id[query_id].text,
                        k=_RECALL_K,
                        search_mode="hybrid",
                    ),
                    user_id=owner,
                    current_context_id=ctx.id,
                    current_workspace_id=ws.id,
                )
                recalls += 1
    finally:
        _restore_env("ENABLE_NEURAL_MEMORY", prev_neural)
    return recalls


def _gold_pairs_from_plan(plan: ReplayPlan) -> set[frozenset[str]]:
    """All within-gold-set doc pairs across every probe (the companions a
    learned layer should associate)."""
    gold_pairs: set[frozenset[str]] = set()
    for probe in plan.probes:
        gold = (probe.seed_doc, *probe.companion_docs)
        for i in range(len(gold)):
            for j in range(i + 1, len(gold)):
                gold_pairs.add(frozenset((gold[i], gold[j])))
    return gold_pairs


async def _seed_edge_calibration(
    db: Any, plan: ReplayPlan, id_map: dict[str, str], ctx: Any
) -> dict[str, Any]:
    """Calibrate the edge_gate threshold from this corpus and seed a row (#982).

    Measures the NON-GOLD pairwise cosine distribution over the corpus vectors
    (the noise population the #118 gate suppresses) and writes a model-global
    ``edge_gate`` calibration row so ``resolve_edge_threshold`` returns
    ``max(percentile, floor)`` for the replay instead of the absolute 0.5. With
    a small fixed fixture the full pairwise distribution is the right estimate
    (random sampling would yield too few pairs). Returns a diagnostic block;
    the caller deletes the row afterward.
    """
    from datetime import timedelta
    from uuid import UUID

    from db.qdrant import get_qdrant_client
    from models.neural import CALIBRATION_KIND_EDGE_GATE
    from neural.calibration import resolve_edge_threshold
    from neural.config import NeuralMemoryConfig
    from neural.utils import cosine_similarity
    from tasks.neural_calibration import _load_measure_script, _upsert_calibration
    from utils.datetime import utcnow

    script = _load_measure_script()
    model_name, dims, collection = await script.resolve_embedding_model(db, ctx.id)
    qdrant = get_qdrant_client()
    vecs_by_mem = await script.fetch_vectors(qdrant, collection, [UUID(m) for m in id_map])
    doc_vec = {id_map[m]: v for m, v in vecs_by_mem.items() if m in id_map}

    gold_pairs = _gold_pairs_from_plan(plan)
    doc_ids = list(doc_vec)
    non_gold_cos: list[float] = []
    gold_cos: list[float] = []
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            a, b = doc_ids[i], doc_ids[j]
            cos = cosine_similarity(doc_vec[a], doc_vec[b])
            if frozenset((a, b)) in gold_pairs:
                gold_cos.append(round(cos, 4))
            else:
                non_gold_cos.append(cos)

    percentiles = script.compute_percentiles(non_gold_cos)
    if not percentiles or not non_gold_cos:
        return {
            "seeded": False,
            "reason": "no_non_gold_pairs",
            "model": model_name,
            "dimensions": dims,
        }

    config = await NeuralMemoryConfig.from_db(db)
    now = utcnow()
    await _upsert_calibration(
        db,
        model_name,
        dims,
        None,
        kind=CALIBRATION_KIND_EDGE_GATE,
        percentiles=percentiles,
        observations=len(non_gold_cos),
        now=now,
        valid_until=now + timedelta(days=config.calibration_ttl_days),
    )
    await db.commit()

    # Past the commit the row EXISTS, so the result must always carry seeded +
    # model + dimensions for the caller's finally cleanup. The diagnostic
    # resolve below is non-essential — guard it so a failure there can never
    # strand the committed transient row (the leak this guards against).
    result: dict[str, Any] = {
        "seeded": True,
        "model": model_name,
        "dimensions": dims,
        "non_gold_observations": len(non_gold_cos),
        "non_gold_percentiles": {k: round(v, 4) for k, v in percentiles.items()},
        "percentile_used": config.min_similarity_for_edge_percentile,
        "floor": config.min_similarity_for_edge_floor,
        "absolute_fallback": config.min_similarity_for_edge,
        "resolved_threshold": None,
        "gold_pair_cosines": sorted(gold_cos),
    }
    try:
        # The exact runtime value the replay will apply (reads the row just written).
        resolved = await resolve_edge_threshold(
            db=db, config=config, model_name=model_name, dimensions=dims
        )
        result["resolved_threshold"] = round(resolved, 4)
    except Exception:  # noqa: BLE001 — diagnostic only; row stays cleanable
        pass
    return result


async def _delete_edge_calibration(db: Any, model_name: str, dimensions: int) -> None:
    """Remove the transient model-global edge_gate row seeded for the demo."""
    from sqlalchemy import delete as sa_delete

    from models.neural import CALIBRATION_KIND_EDGE_GATE, EmbeddingCalibration

    await db.execute(
        sa_delete(EmbeddingCalibration).where(
            EmbeddingCalibration.model_name == model_name,
            EmbeddingCalibration.dimensions == dimensions,
            EmbeddingCalibration.context_id.is_(None),
            EmbeddingCalibration.kind == CALIBRATION_KIND_EDGE_GATE,
        )
    )
    await db.commit()


async def _gate_audit(
    svc: Any,
    db: Any,
    corpus: Corpus,
    plan: ReplayPlan,
    id_map: dict[str, str],
    owner: str,
    ctx: Any,
    ws: Any,
) -> dict[str, Any]:
    """Audit which co-activated pairs the replay traffic *could ever* write.

    Re-runs each replay query through hybrid search (read-only, vectors
    included) and classifies every top-``top_k_coactivation`` pair against
    recall()'s two edge-formation gates — the semantic cosine gate and the
    prune cliff (``delta_w`` here is the first-update Hebbian term
    ``lr · a_i · a_j``; the decay term is zero at weight 0). This makes a
    zero graph-lane lift attributable: if every probe gold pair is gated,
    no amount of replay rounds can produce recovery.
    """
    from neural.calibration import resolve_edge_threshold
    from neural.config import NeuralMemoryConfig
    from neural.utils import cosine_similarity
    from repositories.config_repository import ContextSearchConfigRepository

    config = await NeuralMemoryConfig.from_db(db)
    queries_by_id = {q.id: q for q in corpus.queries}

    gold_pairs = _gold_pairs_from_plan(plan)

    # Pass 1: collect raw pair candidates and capture the embedding dimension.
    # We classify in a second pass so the cosine gate uses the SAME calibrated
    # edge-gate threshold the live replay path applied (#982) — auditing against
    # the absolute config value would mislabel pairs the calibrated gate formed.
    raw_pairs: list[tuple[str, str, str, float | None, float, bool]] = []
    # #983: distinct replay queries that co-recalled each pair — mirrors the
    # runtime's event_key dedup (replaying one query N times is still one
    # observation; only co-recall across DIFFERENT queries is evidence).
    pair_evidence: dict[frozenset[str], set[str]] = {}
    dims: int | None = None
    for query_id in plan.replay_query_ids:
        results = await svc.search_service.hybrid_search(
            query=queries_by_id[query_id].text,
            user_id=owner,
            workspace_id=str(ws.id),
            context_id=str(ctx.id),
            k=_RECALL_K,
            search_mode="hybrid",
            include_vectors=True,
        )
        top = results[: min(config.top_k_coactivation, _RECALL_K, len(results))]
        docs = []
        for result in top:
            # Mirror recall()'s activation: hybrid score clamped to [0, 1].
            score = result.get("hybrid_score", result["score"])
            activation = min(1.0, max(0.0, score))
            docs.append((id_map.get(result["id"], "?"), activation, result.get("embedding") or []))
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                doc_a, act_a, emb_a = docs[i]
                doc_b, act_b, emb_b = docs[j]
                if dims is None and emb_a:
                    dims = len(emb_a)
                cosine = cosine_similarity(emb_a, emb_b) if emb_a and emb_b else None
                delta_w = config.learning_rate * act_a * act_b
                pair_set = frozenset((doc_a, doc_b))
                pair_evidence.setdefault(pair_set, set()).add(query_id)
                raw_pairs.append(
                    (
                        query_id,
                        doc_a,
                        doc_b,
                        cosine,
                        delta_w,
                        pair_set in gold_pairs,
                    )
                )

    # Resolve the calibrated edge-gate threshold (#982). Falls back to the
    # absolute config value when no edge_gate calibration row exists for this
    # (model, dims) — exactly mirroring resolve_edge_threshold's contract and
    # the gate the live replay applied.
    edge_threshold = config.min_similarity_for_edge
    if dims is not None:
        ctx_cfg = await ContextSearchConfigRepository(db).create_or_get(ctx.id)
        edge_threshold = await resolve_edge_threshold(
            db=db, config=config, model_name=ctx_cfg.embedding_model, dimensions=dims
        )

    # 2D edge gate (#983): mirror the runtime's floor clamp — the repetition
    # band can only widen the gate downward, never tighten it.
    edge_floor: float | None = None
    if config.edge_gate_repetition_enabled:
        edge_floor = min(config.min_similarity_for_edge_floor, edge_threshold)

    # Pass 2: classify against the resolved threshold (+ repetition evidence).
    audits = [
        PairAudit(
            query_id=query_id,
            doc_a=doc_a,
            doc_b=doc_b,
            cosine=round(cosine, 4) if cosine is not None else None,
            delta_w=round(delta_w, 4),
            verdict=classify_pair(
                cosine,
                delta_w,
                min_similarity=edge_threshold,
                prune_threshold=config.prune_threshold,
                floor=edge_floor,
                evidence_count=len(pair_evidence.get(frozenset((doc_a, doc_b)), set())),
                min_evidence=config.min_co_activation_count,
            ),
            is_probe_gold_pair=is_gold,
        )
        for (query_id, doc_a, doc_b, cosine, delta_w, is_gold) in raw_pairs
    ]

    summary = summarize_gate_audit(audits)
    summary["thresholds"] = {
        # The value actually applied to classification (calibrated when an
        # edge_gate row exists, else the absolute fallback).
        "min_similarity_for_edge": edge_threshold,
        # Kept for reference so a report shows whether calibration was in effect.
        "min_similarity_for_edge_absolute": config.min_similarity_for_edge,
        # 2D edge gate (#983): the repetition band and its count requirement.
        "edge_gate_repetition_enabled": config.edge_gate_repetition_enabled,
        "min_similarity_for_edge_floor": edge_floor,
        "min_co_activation_count": config.min_co_activation_count,
        "prune_threshold": config.prune_threshold,
        "learning_rate": config.learning_rate,
        "top_k_coactivation": config.top_k_coactivation,
    }
    return summary


async def _run_sleep(db: Any, owner: str, ctx: Any, ws: Any) -> dict[str, Any]:
    """One Sleep consolidation pass (edges_only) over the eval context.

    The LLM judge is disabled (``sleep_llm_provider=""`` → auto-accept path)
    so the run is deterministic and free — edge discovery still performs the
    real Qdrant similarity sweep and persists ``semantic``-origin edges.
    Failure is recorded, not raised: the warm_sleep checkpoint still runs and
    the results JSON carries the honest sleep status.
    """
    from dataclasses import replace

    from sqlalchemy import select

    from models.sleep import SleepReport
    from neural.config import NeuralMemoryConfig
    from services.sleep.orchestrator import SleepOrchestrator

    NeuralMemoryConfig.invalidate_cache()
    config = replace(await NeuralMemoryConfig.from_db(db), sleep_llm_provider="")
    try:
        await SleepOrchestrator(db).run(
            owner, workspace_id=str(ws.id), context_id=str(ctx.id), config=config
        )
    except Exception as exc:  # noqa: BLE001 — recorded in results, not fatal
        await db.rollback()
        return {"ok": False, "error": str(exc)}

    report = (
        await db.execute(
            select(SleepReport)
            .where(SleepReport.user_id == owner)
            .order_by(SleepReport.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if report is None:
        return {"ok": False, "error": "sleep run left no report"}
    return {
        "ok": report.status == "completed",
        "status": report.status,
        "edge_discovery": report.edge_discovery_result,
    }


async def _edge_stats(db: Any, ctx_id: Any) -> dict[str, Any]:
    """Per-origin edge count + mean weight for the eval context."""
    from sqlalchemy import func, select

    from models.memory import NeuralMemoryEdge

    rows = (
        await db.execute(
            select(
                NeuralMemoryEdge.origin,
                func.count().label("count"),
                func.avg(NeuralMemoryEdge.weight).label("avg_weight"),
            )
            .where(NeuralMemoryEdge.context_id == ctx_id)
            .group_by(NeuralMemoryEdge.origin)
        )
    ).all()
    return {
        origin: {"count": count, "avg_weight": round(float(avg_weight), 4)}
        for origin, count, avg_weight in rows
    }


def _restore_env(name: str, prev: str | None) -> None:
    if prev is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = prev


def _main() -> int:
    import asyncio

    results = asyncio.run(run_compounding_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
