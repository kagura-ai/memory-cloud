"""Live #1220 router-calibration-gate orchestration (stage 3 + 4 of #1212).

Ingests the frozen corpus once, then measures THREE paired lane arms over
ALL corpus queries — semantic-only, keyword-only, hybrid — each pinned via
an explicit ``search_mode`` (an explicit mode always wins, so no config
flip is needed). The ROUTED arm is then constructed, not re-measured: the
classifier is deterministic, so query *i*'s routed ranking IS
``arms[classify_query(q_i).lane][i]``. The pre-declared contracts live in
``tests.eval.router_gate``; a per-bucket breakdown of every arm is
persisted to the ``router_calibrations`` store (stage 4, fleet-default
scope ``context_id IS NULL``).

Arms are measured with ``ENABLE_NEURAL_MEMORY=false`` and
``KAGURA_GRAPH_BOOST_ENABLED=false`` (same discipline as replay_runner: the
ONLY variable between paired arms is the retrieval lane).

Writes results/router-calibration-<YYYY-MM-DD>.json from a REAL run only.
Heavy imports are function-local (same convention as placebo_runner) so
importing this module is cheap and CI-safe.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.eval.router_gate import (
    COMPONENT_ARMS,
    arm_metrics,
    bucket_indices,
    evaluate_router_gate,
)
from tests.eval.runner import _ingest_corpus, _teardown
from tests.eval.tools.corpus import load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
#: The frozen kagura_l corpus (300 docs, multi-gold probes) — NOT the
#: 5-probe golden corpus: the gate's BCa flip decision needs
#: n >= router_gate.MIN_QUERIES (gate2/CAIO).
_CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "kagura_l.yaml"
_RECALL_K = 10
#: Pre-declared bootstrap seed for the gate's BCa intervals.
_GATE_SEED = 1220
#: The lane arms measured live; "routed" is constructed from these.
_LANE_ARMS = ("semantic", "keyword", "hybrid")


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


async def _lane_rankings(
    svc: Any,
    queries: list[tuple[str, set[str]]],
    id_map: dict[str, str],
    owner: str,
    ctx_id: Any,
    ws_id: Any,
    search_mode: str,
) -> list[tuple[list[str], set[str]]]:
    """Recall each (query_text, gold_doc_ids) pair under ONE explicit lane."""
    from models.schemas import RecallRequest

    rankings: list[tuple[list[str], set[str]]] = []
    for text, gold in queries:
        resp = await svc.recall(
            request=RecallRequest(query=text, k=_RECALL_K, search_mode=search_mode),
            user_id=owner,
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        rankings.append(([id_map.get(str(r.memory_id), "?") for r in resp.results], set(gold)))
    return rankings


async def _persist_calibrations(
    db: Any,
    components: dict[str, list[tuple[list[str], set[str]]]],
    routed: list[tuple[list[str], set[str]]],
    lanes: list[str],
) -> int:
    """Stage 4: upsert per-bucket arm performance as fleet defaults."""
    from datetime import UTC, datetime

    from repositories.config_repository import RouterCalibrationRepository

    repo = RouterCalibrationRepository(db)
    sampled_at = datetime.now(UTC)
    all_arms: dict[str, list[tuple[list[str], set[str]]]] = {**components, "routed": routed}
    written = 0
    for bucket, indices in sorted(bucket_indices(lanes).items()):
        for arm, rankings in all_arms.items():
            stats = arm_metrics([rankings[i] for i in indices])
            await repo.upsert(
                bucket=bucket,
                arm=arm,
                p_at_5=float(stats["p@5"]),
                mrr_at_10=float(stats["mrr@10"]),
                n_queries=int(stats["n"]),
                sampled_at=sampled_at,
                context_id=None,
            )
            written += 1
    await db.commit()
    return written


async def run_router_calibration_eval(
    write: bool = True, persist: bool = True, run_date: str | None = None
) -> dict[str, Any]:
    """Orchestrate the lane-arm measurement, apply the gate, persist store rows."""
    from datetime import date

    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name
    from services.memory_service import MemoryService
    from services.query_router import classify_query
    from tests.eval._provisioning import provision_eval_context

    corpus = load_corpus(_CORPUS_PATH)
    queries = [(q.text, set(q.relevant)) for q in corpus.queries]
    lanes = [classify_query(text).lane for text, _ in queries]

    async for db in get_db():
        owner, ws, ctx, emb_model, emb_dims = await provision_eval_context(db)
        svc = MemoryService(db)
        id_map: dict[str, str] = {}
        try:
            await ensure_kagura_memories_collection(
                emb_dims, get_collection_name(emb_model, emb_dims)
            )
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)

            # Paired lane arms: the ONLY variable is search_mode.
            components: dict[str, list[tuple[list[str], set[str]]]] = {}
            with _env("ENABLE_NEURAL_MEMORY", "false"):
                with _env("KAGURA_GRAPH_BOOST_ENABLED", "false"):
                    for mode in _LANE_ARMS:
                        components[mode] = await _lane_rankings(
                            svc, queries, id_map, owner, ctx.id, ws.id, mode
                        )

            # The router is deterministic → the routed arm is constructed.
            routed = [components[lane][i] for i, lane in enumerate(lanes)]
            gate = evaluate_router_gate(
                routed=routed,
                components=components,
                lanes=lanes,
                seed=_GATE_SEED,
            )
            calibrations_written = (
                await _persist_calibrations(db, components, routed, lanes) if persist else 0
            )
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))
        break
    else:
        raise RuntimeError("database session unavailable — is the stack up?")

    result: dict[str, Any] = {
        "n_queries": len(queries),
        "lane_mix": {lane: len(idx) for lane, idx in sorted(bucket_indices(lanes).items())},
        "gate": gate,
        "calibrations_written": calibrations_written,
        "meta": {
            "issue": 1220,
            "date": run_date or date.today().isoformat(),
            "corpus": corpus.meta,
            "gate_seed": _GATE_SEED,
            "recall_k": _RECALL_K,
            "component_arms": list(COMPONENT_ARMS),
        },
    }
    if write:
        _RESULTS_DIR.mkdir(exist_ok=True)
        out = _RESULTS_DIR / f"router-calibration-{result['meta']['date']}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _main() -> int:
    import asyncio

    results = asyncio.run(run_router_calibration_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
