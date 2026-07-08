"""Live #1213 graph-boost placebo-gate orchestration.

Builds ONE warm co-activation graph (provisional τ → replay → sleep, the
placebo_runner procedure), then measures FOUR recall arms off it:

    unboosted        — companion probes, KAGURA_GRAPH_BOOST_ENABLED=false
    boosted_real     — same probes, boost ON, true warm graph
    boosted_rewired  — same probes, boost ON, degree-preserving rewired graph
                       (edges restored from snapshot afterwards)
    non-graph slice  — the corpus's replay queries, boost ON vs OFF
                       (the fusion-dilution non-inferiority check)

and applies the pre-declared contracts in ``tests.eval.graph_boost_gate``.
Arms are measured with ``ENABLE_NEURAL_MEMORY=false`` (same discipline as
replay_runner: no Hebbian writes may contaminate the graph between paired
arms — the ONLY graph influence on ranking is the boost under test).

Writes results/graph-boost-<YYYY-MM-DD>.json from a REAL run only. Heavy
imports are function-local (same convention as placebo_runner) so importing
this module is cheap and CI-safe.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.eval.compounding import MODE_EXCLUDE_PROBES, build_replay_plan
from tests.eval.graph_boost_gate import evaluate_gate
from tests.eval.placebo import Edge, degree_preserving_rewire_with_stats
from tests.eval.placebo_runner import (
    _replace_edges,
    _seed_provisional_tau,
    _snapshot_edges,
)
from tests.eval.replay_runner import _replay, _run_sleep
from tests.eval.runner import _ingest_corpus, _teardown
from tests.eval.tools.corpus import load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
#: The frozen kagura_l corpus (300 docs, 60 multi-gold cross-source probes) —
#: NOT the 5-probe golden corpus: the gate's BCa ship decision needs
#: n >= graph_boost_gate.MIN_PROBES (gate2/CAIO).
_CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "kagura_l.yaml"
_RECALL_K = 10
#: Pre-declared bootstrap seed for the gate's BCa intervals.
_GATE_SEED = 1213
#: Pre-declared rewire seeds: the beats-placebo contract must hold against
#: EVERY rewiring (sparse graphs make any single rewiring a weak null).
_REWIRE_SEEDS = (42, 43, 44)


@contextmanager
def _env(key: str, value: str):
    """Set an env var for the duration, restoring the prior state exactly."""
    prior = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


async def _recall_rankings(
    svc: Any,
    queries: list[tuple[str, set[str]]],
    id_map: dict[str, str],
    owner: str,
    ctx_id: Any,
    ws_id: Any,
) -> list[tuple[list[str], set[str]]]:
    """Recall each (query_text, gold_doc_ids) pair and map results to doc ids."""
    from models.schemas import RecallRequest

    rankings: list[tuple[list[str], set[str]]] = []
    for text, gold in queries:
        resp = await svc.recall(
            request=RecallRequest(query=text, k=_RECALL_K, search_mode="hybrid"),
            user_id=owner,
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        rankings.append(([id_map.get(str(r.memory_id), "?") for r in resp.results], set(gold)))
    return rankings


async def run_graph_boost_eval(write: bool = True, run_date: str | None = None) -> dict[str, Any]:
    """Orchestrate the four-arm measurement and apply the gate."""
    from datetime import date

    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name
    from services.memory_service import MemoryService
    from tests.eval._provisioning import provision_eval_context

    corpus = load_corpus(_CORPUS_PATH)
    plan = build_replay_plan(corpus, MODE_EXCLUDE_PROBES)

    async for db in get_db():
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
            result = await _measure_arms(svc, db, corpus, plan, id_map, owner, ctx, ws)
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))
        break
    else:
        raise RuntimeError("database session unavailable — is the stack up?")

    result["meta"] = {
        "issue": 1213,
        "date": run_date or date.today().isoformat(),
        "corpus": corpus.meta,
        "gate_seed": _GATE_SEED,
        "rewire_seeds": list(_REWIRE_SEEDS),
        "recall_k": _RECALL_K,
        "graph_boost_max": os.getenv("KAGURA_GRAPH_BOOST_MAX", "0.15"),
    }
    if write:
        _RESULTS_DIR.mkdir(exist_ok=True)
        out = _RESULTS_DIR / f"graph-boost-{result['meta']['date']}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


async def _measure_arms(svc, db, corpus, plan, id_map, owner, ctx, ws) -> dict[str, Any]:
    """Warm the graph once, then run the four paired arms."""
    # --- warm build: provisional τ → replay (neural ON) → sleep -------------
    tau_info = await _seed_provisional_tau(db, corpus, plan, id_map, ctx)
    with _env("ENABLE_NEURAL_MEMORY", "true"), _env("KAGURA_GRAPH_BOOST_ENABLED", "false"):
        replayed = await _replay(svc, corpus, plan, owner, ctx, ws)
        sleep_info = await _run_sleep(db, owner, ctx, ws)

    # --- paired query sets ---------------------------------------------------
    probe_queries = [(p.text, set(p.companion_docs)) for p in plan.probes]
    replay_ids = set(plan.replay_query_ids)
    nongraph_queries = [(q.text, set(q.relevant)) for q in corpus.queries if q.id in replay_ids]

    # --- arms (neural OFF: no Hebbian writes between paired measurements) ----
    with _env("ENABLE_NEURAL_MEMORY", "false"):
        with _env("KAGURA_GRAPH_BOOST_ENABLED", "false"):
            unboosted = await _recall_rankings(svc, probe_queries, id_map, owner, ctx.id, ws.id)
            nongraph_off = await _recall_rankings(
                svc, nongraph_queries, id_map, owner, ctx.id, ws.id
            )
        with _env("KAGURA_GRAPH_BOOST_ENABLED", "true"):
            boosted_real = await _recall_rankings(svc, probe_queries, id_map, owner, ctx.id, ws.id)
            nongraph_on = await _recall_rankings(
                svc, nongraph_queries, id_map, owner, ctx.id, ws.id
            )

        # --- placebo arms: rewire ONLY the hebbian edges (the boost reads
        # only hebbian — rewiring semantic/declared edges too would make the
        # null model inconsistent with the mechanism under test), one
        # rewiring per pre-declared seed, snapshot restored afterwards.
        snapshot = await _snapshot_edges(db, ctx.id)
        hebbian_rows = [r for r in snapshot if r["origin"] == "hebbian"]
        other_rows = [r for r in snapshot if r["origin"] != "hebbian"]
        boosted_rewired_arms: dict[int, Any] = {}
        rewire_swaps_by_seed: dict[int, int] = {}
        if len(hebbian_rows) >= 2:
            edges = [
                Edge(
                    str(r["src_id"]),
                    str(r["dst_id"]),
                    r["weight"],
                    r["origin"],
                    r["confidence"],
                    r["edge_type"],
                )
                for r in hebbian_rows
            ]
            try:
                for rewire_seed in _REWIRE_SEEDS:
                    rewired, swaps = degree_preserving_rewire_with_stats(edges, seed=rewire_seed)
                    rewire_swaps_by_seed[rewire_seed] = swaps
                    rewired_rows = other_rows + [
                        {
                            "src_id": UUID(e.src),
                            "dst_id": UUID(e.dst),
                            "weight": e.weight,
                            "confidence": e.confidence,
                            "edge_type": e.edge_type,
                            "origin": e.origin,
                            "user_id": owner,
                            "workspace_id": ws.id,
                            "context_id": ctx.id,
                        }
                        for e in rewired
                    ]
                    await _replace_edges(db, ctx.id, rewired_rows)
                    with _env("KAGURA_GRAPH_BOOST_ENABLED", "true"):
                        boosted_rewired_arms[rewire_seed] = await _recall_rankings(
                            svc, probe_queries, id_map, owner, ctx.id, ws.id
                        )
            finally:
                await _replace_edges(db, ctx.id, snapshot)
        else:
            boosted_rewired_arms = dict.fromkeys(_REWIRE_SEEDS, unboosted)

    gate = evaluate_gate(
        boosted_real=boosted_real,
        unboosted=unboosted,
        boosted_rewired_arms=boosted_rewired_arms,
        nongraph_boosted=nongraph_on,
        nongraph_unboosted=nongraph_off,
        seed=_GATE_SEED,
    )
    return {
        "warm_build": {
            "tau": tau_info,
            "replayed_queries": replayed,
            "sleep": sleep_info,
            "edge_snapshot": {
                "n": len(snapshot),
                "n_hebbian": len(hebbian_rows),
                "rewire_swaps_by_seed": rewire_swaps_by_seed,
            },
        },
        "n_probes": len(probe_queries),
        "n_nongraph": len(nongraph_queries),
        # gate2/CAIO: the non-inferiority slice is the corpus's replay
        # queries — the SAME queries the warm build replayed, so the boost
        # re-ranks exactly their co-activated results. Leaky-optimistic, not
        # conservative; the frozen held-out retrieval slice is the honest
        # follow-up before any per-context graduation.
        "nongraph_slice": "replay_queries (leaky-optimistic; see docs/eval/graph-boost-gate.md)",
        "gate": gate,
    }


def _main() -> int:
    import asyncio

    results = asyncio.run(run_graph_boost_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
