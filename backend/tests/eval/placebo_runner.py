"""Live Day-2 placebo kill-shot orchestration (directional de-risk).

Builds ONE warm graph at a provisional τ (prereg-v1 §3 procedure on the current
corpus), then measures companion recovery@10 for three arms off it:

    real-warm            — true warm graph, true gold
    shuffled-gold        — same rankings, size-preserving permuted gold
    random-edge placebo  — degree-preserving rewired graph, true gold

Reports paired point-estimate deltas with DESCRIPTIVE bootstrap intervals. This
is NOT the inferential kill-shot (no gated CI, no accept/reject, τ untagged) —
that runs at Day-3 on the grown corpus at the single committed τ.

Writes results/placebo-<YYYY-MM-DD>.json from a REAL run only. Heavy imports are
function-local (same convention as replay_runner) so importing this module is
cheap and CI-safe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.eval.compounding import MODE_EXCLUDE_PROBES, build_replay_plan
from tests.eval.placebo import (
    Edge,
    degree_preserving_rewire_with_stats,
    median_cross_topic_gold_pair_cosine,
    paired_delta_bootstrap,
    permute_gold,
    recovery_from_rankings,
)
from tests.eval.replay_runner import (
    _EXPLORE_DEPTH,
    _EXPLORE_MIN_WEIGHT,
    _REPLAY_ROUNDS,
    _gold_pairs_from_plan,
    _replay,
    _run_sleep,
)
from tests.eval.runner import _ingest_corpus, _sudachi_version, _teardown
from tests.eval.tools.corpus import load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_DELTA_COMPOUND = 0.10  # directional keep/pivot threshold (prereg δ_compound)


async def run_placebo_eval(
    write: bool = True,
    run_date: str | None = None,
    seeds: tuple[int, ...] = (1, 2, 3),
    rounds: int = _REPLAY_ROUNDS,
    mode: str = MODE_EXCLUDE_PROBES,
) -> dict[str, Any]:
    from utils.datetime import utcnow

    corpus = load_corpus()
    run_date = run_date or utcnow().strftime("%Y-%m-%d")
    plan = build_replay_plan(corpus, mode=mode, rounds=rounds)

    block = await _run_placebo_mode(corpus, plan, seeds)

    probe_count = len(plan.probes)
    results: dict[str, Any] = {
        "run_date": run_date,
        "experiment": "day2-placebo",
        "issue": None,
        "sudachi_version": _sudachi_version(),
        "corpus_version": corpus.meta.get("version"),
        "doc_count": len(corpus.documents),
        "query_count": len(corpus.queries),
        "probe_count": probe_count,
        "probe_underpowered": probe_count < 50,
        "rounds": rounds,
        "explore_depth": _EXPLORE_DEPTH,
        "explore_min_weight": _EXPLORE_MIN_WEIGHT,
        "seeds": list(seeds),
        "design_note": (
            "Descriptive/directional de-risk (week1-derisk Day 2). Provisional τ "
            "(prereg §3 procedure on the CURRENT corpus), untagged. Percentile "
            "bootstrap intervals are shape aids, NOT gated CIs; no accept/reject. "
            "The inferential H2 kill-shot runs at Day-3 (prereg-v1-frozen)."
        ),
        **block,
    }
    if probe_count < 50:
        print(
            f"eval-placebo: WARNING probe_count={probe_count} << 50 — directional read only, underpowered"
        )

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / f"placebo-{run_date}.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"eval-placebo: wrote {out}")
    return results


async def _run_placebo_mode(corpus: Any, plan: Any, seeds: tuple[int, ...]) -> dict[str, Any]:
    """Provision an isolated workspace, build the warm graph once, run every
    seed's placebo arms off it, tear down."""
    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name
    from services.memory_service import MemoryService
    from tests.eval._provisioning import provision_eval_context

    async for db in get_db():
        # Shared eval provisioning (#19); edges_only so the placebo edge-graph
        # snapshot/rewire path has a co-activation graph to sample.
        owner, ws, ctx, emb_model, emb_dims = await provision_eval_context(
            db, sleep_mode="edges_only"
        )

        svc = MemoryService(db)
        id_map: dict[str, str] = {}
        try:
            await ensure_kagura_memories_collection(
                emb_dims, get_collection_name(emb_model, emb_dims)
            )
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)
            return await _measure_placebo(svc, db, corpus, plan, id_map, owner, ctx, ws, seeds)
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))

    raise RuntimeError("database session unavailable — is the stack up?")


async def _measure_placebo(svc, db, corpus, plan, id_map, owner, ctx, ws, seeds) -> dict[str, Any]:
    seed_mem_by_doc = {doc_id: mem_id for mem_id, doc_id in id_map.items()}
    true_gold = {p.query_id: p.companion_docs for p in plan.probes}

    # --- warm build (once, seed-independent): provisional τ -> replay -> sleep ---
    tau_info: dict[str, Any] = {"seeded": False}
    sleep_info: dict[str, Any]
    try:
        tau_info = await _seed_provisional_tau(db, corpus, plan, id_map, ctx)
        await _replay(svc, corpus, plan, owner, ctx, ws)
        sleep_info = await _run_sleep(db, owner, ctx, ws)
        real_rankings, seeds_in_graph = await _explore_rankings(
            svc, plan.probes, seed_mem_by_doc, id_map, owner, ctx, ws
        )
        real_warm = recovery_from_rankings(real_rankings, true_gold)
        snapshot = await _snapshot_edges(db, ctx.id)

        per_seed = []
        for s in seeds:
            per_seed.append(
                await _placebo_arms_for_seed(
                    svc,
                    db,
                    plan,
                    id_map,
                    seed_mem_by_doc,
                    owner,
                    ctx,
                    ws,
                    real_rankings,
                    real_warm,
                    true_gold,
                    snapshot,
                    s,
                )
            )
    finally:
        if tau_info.get("seeded"):
            await _restore_provisional_tau(
                db,
                tau_info["model"],
                tau_info["dimensions"],
                tau_info.get("prior_calibration"),
            )

    d_rand = sorted(
        b["deltas"]["random_edge"]["delta"] for b in per_seed if b["deltas"].get("random_edge")
    )
    d_shuf = sorted(b["deltas"]["shuffled_gold"]["delta"] for b in per_seed)
    across = {
        "delta_random_edge": _min_med_max(d_rand),
        "delta_shuffled_gold": _min_med_max(d_shuf),
    }
    return {
        "tau": tau_info,
        "seeds_in_graph": seeds_in_graph,
        "sleep": sleep_info,
        "real_warm": real_warm,
        "edge_count": len(snapshot),
        "per_seed": per_seed,
        "across_seeds": across,
        "directional_read": _directional_read(real_warm, across),
    }


async def _placebo_arms_for_seed(
    svc,
    db,
    plan,
    id_map,
    seed_mem_by_doc,
    owner,
    ctx,
    ws,
    real_rankings,
    real_warm,
    true_gold,
    snapshot,
    seed,
) -> dict[str, Any]:
    # shuffled-gold: same real-warm rankings, permuted gold
    shuffled_map = permute_gold(plan.probes, seed=seed)
    shuffled = recovery_from_rankings(real_rankings, shuffled_map)

    # random-edge: degree-preserving rewire of the snapshot, re-explore, restore
    if len(snapshot) < 2:
        random_edge: dict[str, Any] = {"n": 0, "reason": "graph_too_sparse_to_rewire"}
        rewire_swaps = 0
    else:
        placebo_edges = [
            Edge(
                str(r["src_id"]),
                str(r["dst_id"]),
                r["weight"],
                r["origin"],
                r["confidence"],
                r["edge_type"],
            )
            for r in snapshot
        ]
        # Use the accepted-swap count, NOT len(rewired) (== edge count): a run
        # that accepts zero swaps must be distinguishable from a fully-mixed one
        # in edge_snapshot.rewire_swaps_done (v0.42 review).
        rewired, rewire_swaps = degree_preserving_rewire_with_stats(placebo_edges, seed=seed)
        template = {"user_id": owner, "workspace_id": ws.id, "context_id": ctx.id}
        rewired_rows = [
            {
                "src_id": UUID(e.src),
                "dst_id": UUID(e.dst),
                "weight": e.weight,
                "confidence": e.confidence,
                "origin": e.origin,
                "edge_type": e.edge_type,
                **template,
            }
            for e in rewired
        ]
        await _replace_edges(db, ctx.id, rewired_rows)
        try:
            re_rankings, _ = await _explore_rankings(
                svc, plan.probes, seed_mem_by_doc, id_map, owner, ctx, ws
            )
            random_edge = recovery_from_rankings(re_rankings, true_gold)
        finally:
            await _replace_edges(db, ctx.id, snapshot)  # always restore the true warm graph

    deltas: dict[str, Any] = {
        "shuffled_gold": paired_delta_bootstrap(
            real_warm["per_probe@10"], shuffled["per_probe@10"], seed=seed
        ),
        "random_edge": (
            paired_delta_bootstrap(
                real_warm["per_probe@10"], random_edge["per_probe@10"], seed=seed
            )
            if random_edge.get("n")
            else None
        ),
    }
    return {
        "seed": seed,
        "arms": {"real_warm": real_warm, "shuffled_gold": shuffled, "random_edge": random_edge},
        "deltas": deltas,
        "gold_permutation": {k: list(v) for k, v in shuffled_map.items()},
        "edge_snapshot": {"count": len(snapshot), "rewire_swaps_done": rewire_swaps},
    }


async def _explore_rankings(svc, probes, seed_mem_by_doc, id_map, owner, ctx, ws):
    """Explore from each probe seed; return [(query_id, ranked_docs)] in probe
    order + a seeds-in-graph count. Mirrors replay_runner._measure_graph_lane's
    explore loop but returns the raw rankings so all arms can re-score them."""
    from models.schemas import ExploreRequest

    out: list[tuple[str, list[str]]] = []
    seeds_in_graph = 0
    for probe in probes:
        resp = await svc.explore(
            request=ExploreRequest(
                memory_id=UUID(seed_mem_by_doc[probe.seed_doc]),
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
        out.append((probe.query_id, [id_map.get(str(m.memory_id), "?") for m in related]))
    return out, seeds_in_graph


async def _snapshot_edges(db, ctx_id) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from models.memory import NeuralMemoryEdge

    rows = (
        (await db.execute(select(NeuralMemoryEdge).where(NeuralMemoryEdge.context_id == ctx_id)))
        .scalars()
        .all()
    )
    return [
        {
            "src_id": r.src_id,
            "dst_id": r.dst_id,
            "weight": r.weight,
            "confidence": r.confidence,
            "origin": r.origin,
            "edge_type": r.edge_type,
            "user_id": r.user_id,
            "workspace_id": r.workspace_id,
            "context_id": r.context_id,
        }
        for r in rows
    ]


async def _replace_edges(db, ctx_id, rows: list[dict[str, Any]]) -> None:
    from sqlalchemy import delete as sa_delete

    from models.memory import NeuralMemoryEdge

    await db.execute(sa_delete(NeuralMemoryEdge).where(NeuralMemoryEdge.context_id == ctx_id))
    for r in rows:
        db.add(
            NeuralMemoryEdge(**r)
        )  # fresh autoincrement id; created_at/last_updated default now()
    await db.commit()


async def _seed_provisional_tau(db, corpus, plan, id_map, ctx) -> dict[str, Any]:
    """Compute provisional τ (median cross-topic gold-pair cosine) and seed a
    transient model-global edge_gate calibration row so resolve_edge_threshold
    returns max(τ, floor). Mirrors replay_runner._seed_edge_calibration but
    substitutes τ for the non-gold percentile: compute_percentiles([τ]*N) makes
    every stored percentile τ, so percentile(95) == τ."""
    from datetime import timedelta

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
    source_by_doc = {d.id: d.source for d in corpus.documents}
    gold_pairs = _gold_pairs_from_plan(plan)

    tau = median_cross_topic_gold_pair_cosine(
        doc_vec, gold_pairs, source_by_doc, cosine_fn=cosine_similarity
    )
    cross_topic_count = sum(
        1 for pr in gold_pairs if len(pr) == 2 and len({source_by_doc.get(d) for d in pr}) == 2
    )
    if tau is None:
        return {
            "seeded": False,
            "reason": "no_cross_topic_gold_pair",
            "model": model_name,
            "dimensions": dims,
            "tau_provisional": None,
            "cross_topic_gold_pair_count": cross_topic_count,
        }

    # Snapshot any PRE-EXISTING production model-global edge_gate row BEFORE the
    # upsert clobbers it. _upsert_calibration is a kind-scoped delete-then-insert,
    # and this (model, dims, context_id IS NULL, kind) key is the exact one the
    # production calibration subsystem writes — so without capturing it here, the
    # eval would overwrite live edge-gating during the run and _restore would
    # delete it outright at teardown, leaving production on the uncalibrated
    # default until the next recalibration (v0.42 review).
    prior_calibration = await _snapshot_model_global_edge_gate(db, model_name, dims)

    percentiles = script.compute_percentiles([tau] * 128)
    config = await NeuralMemoryConfig.from_db(db)
    now = utcnow()
    await _upsert_calibration(
        db,
        model_name,
        dims,
        None,
        kind=CALIBRATION_KIND_EDGE_GATE,
        percentiles=percentiles,
        observations=cross_topic_count,
        now=now,
        valid_until=now + timedelta(days=config.calibration_ttl_days),
    )
    await db.commit()

    result: dict[str, Any] = {
        "seeded": True,
        "prior_calibration": prior_calibration,
        "model": model_name,
        "dimensions": dims,
        "tau_provisional": round(tau, 4),
        "tau_method": "median_cross_topic_gold_pair_cosine",
        "cross_topic_gold_pair_count": cross_topic_count,
        "floor": config.min_similarity_for_edge_floor,
        "resolved_edge_threshold": None,
    }
    try:
        resolved = await resolve_edge_threshold(
            db=db, config=config, model_name=model_name, dimensions=dims
        )
        result["resolved_edge_threshold"] = round(resolved, 4)
    except Exception:  # noqa: BLE001 — diagnostic only; row stays cleanable
        pass
    return result


async def _snapshot_model_global_edge_gate(db, model_name, dimensions) -> dict[str, Any] | None:
    """Capture the current production model-global edge_gate calibration row (if
    any) as plain values, so a placebo run can restore it after teardown."""
    from sqlalchemy import select

    from models.neural import CALIBRATION_KIND_EDGE_GATE, EmbeddingCalibration

    row = (
        await db.execute(
            select(EmbeddingCalibration).where(
                EmbeddingCalibration.model_name == model_name,
                EmbeddingCalibration.dimensions == dimensions,
                EmbeddingCalibration.context_id.is_(None),
                EmbeddingCalibration.kind == CALIBRATION_KIND_EDGE_GATE,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "percentiles": {
            "p25": row.p25,
            "p50": row.p50,
            "p75": row.p75,
            "p90": row.p90,
            "p95": row.p95,
            "p99": row.p99,
        },
        "sample_size": row.sample_size,
        "sampled_at": row.sampled_at,
        "valid_until": row.valid_until,
    }


async def _restore_provisional_tau(db, model_name, dimensions, prior) -> None:
    """Delete the placebo edge_gate row and restore the pre-existing production
    row captured at seed time. Deleting outright (the old behavior) destroyed a
    real production calibration for (model, dims), silently reverting live
    edge-gating to the uncalibrated default (v0.42 review)."""
    from sqlalchemy import delete as sa_delete

    from models.neural import CALIBRATION_KIND_EDGE_GATE, EmbeddingCalibration
    from tasks.neural_calibration import _upsert_calibration

    await db.execute(
        sa_delete(EmbeddingCalibration).where(
            EmbeddingCalibration.model_name == model_name,
            EmbeddingCalibration.dimensions == dimensions,
            EmbeddingCalibration.context_id.is_(None),
            EmbeddingCalibration.kind == CALIBRATION_KIND_EDGE_GATE,
        )
    )
    if prior is not None:
        # Re-insert the original production row verbatim (same percentiles,
        # sample_size, sampled_at, valid_until) via the canonical upsert.
        await _upsert_calibration(
            db,
            model_name,
            dimensions,
            None,
            kind=CALIBRATION_KIND_EDGE_GATE,
            percentiles=prior["percentiles"],
            observations=prior["sample_size"],
            now=prior["sampled_at"],
            valid_until=prior["valid_until"],
        )
    await db.commit()


def _min_med_max(values: list[float]) -> dict[str, float | None]:
    from statistics import median

    if not values:
        return {"min": None, "median": None, "max": None}
    return {
        "min": round(min(values), 4),
        "median": round(median(values), 4),
        "max": round(max(values), 4),
    }


def _directional_read(real_warm: dict[str, Any], across: dict[str, Any]) -> str:
    med_rand = across["delta_random_edge"]["median"]
    med_shuf = across["delta_shuffled_gold"]["median"]
    if med_rand is None:
        return "inconclusive"
    if med_rand >= _DELTA_COMPOUND and med_shuf is not None and med_shuf >= _DELTA_COMPOUND:
        return "alive"
    if med_rand <= 0.0:
        return "edge_spray"
    return "inconclusive"


def _main() -> int:
    import asyncio

    results = asyncio.run(run_placebo_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
