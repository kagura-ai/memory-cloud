"""Live reinforce ON-vs-OFF eval orchestration (Issue #1069).

Tier-C harness: the #344 ``runner.py`` measures *static* retrieval quality and
the #969 ``replay_runner.py`` measures graph *compounding*; this one measures
whether enabling the bounded reinforce re-rank (#1048) on a context is **safe** —
the eval the staged production rollout is gated on.

Protocol (one ingested corpus, one A/B flip — only the config changes):

    ingest reinforce_corpus
      → seed adoption (reference_count) + net-helpful feedback on the canonical
        current-fact docs (meta.adopted_docs)
      → OFF arm: reinforce pinned OFF explicitly (#1207 made a materialized
        config row default to enabled, and recall materializes the row via
        create_or_get — so "no row yet" no longer means OFF) → score
        current_fact / rare + the zero-adoption surfacing rate
      → flip ContextSearchConfig.reinforce_enabled = true
      → ON arm: score the same queries again
      → evaluate_reinforce_gate(off, on)  → results/reinforce-<date>.json

The decision logic + metrics are pure (``reinforce_gate.py``, unit-tested); this
module is the live driver. Heavy imports are function-local so importing the
module stays cheap and CI-safe. Produces JSON from a REAL run only — never
fabricated. Run via ``make eval-reinforce`` (needs the live stack).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tests.eval.reinforce_gate import (
    POPULATIONS,
    ArmBlock,
    GateThresholds,
    evaluate_reinforce_gate,
    population_metrics,
    zero_adoption_surfacing_rate,
)
from tests.eval.runner import _ingest_corpus, _teardown
from tests.eval.tools.corpus import Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_REINFORCE_CORPUS = Path(__file__).resolve().parent / "fixtures" / "reinforce_corpus.yaml"
_RECALL_K = 10
_MAX_BOOST = 0.15
# Adoption seeding for the canonical current-fact docs before the ON arm: each
# adopted doc gets _SEED_REFERENCES reference() bumps (adoption signal) and
# _SEED_HELPFUL net-helpful feedback events. Enough to clear the log-scaled
# adoption term meaningfully without saturating it (the factor is bounded anyway).
_SEED_REFERENCES = 5
_SEED_HELPFUL = 3


def _load_reinforce_corpus() -> Corpus:
    return load_corpus(_REINFORCE_CORPUS)


async def _seed_adoption(
    svc: Any,
    ctx_id: UUID,
    owner: str,
    memory_ids: list[UUID],
    *,
    references: int = _SEED_REFERENCES,
    helpful: int = _SEED_HELPFUL,
) -> None:
    """Seed adoption (reference_count) + net-helpful feedback on the canonical docs.

    ``reference()`` is the adoption signal (#1046): each call bumps
    ``reference_count`` by one. ``record_feedback(helpful=True)`` is the retrieval
    feedback signal (#888). Together these are exactly the inputs the reinforce
    factor reads, so the ON arm has real signal to act on while the rare
    (zero-adoption) docs stay untouched.
    """
    from services.feedback_service import FeedbackService

    fb = FeedbackService(svc.db)
    for mid in memory_ids:
        for _ in range(references):
            await svc.reference(mid, owner)
        for _ in range(helpful):
            await fb.record_feedback(ctx_id, mid, helpful=True, user_id=owner)
    await svc.db.commit()


async def _set_reinforce(db: Any, ctx_id: UUID, *, enabled: bool, max_boost: float) -> None:
    """Flip ``ContextSearchConfig.reinforce_enabled`` for the eval context.

    Sets the attribute directly on the (created-if-missing) config row — the
    ``ContextSearchConfigUpdate`` schema requires the full weight set, which we do
    not want to disturb for an A/B that only toggles reinforce.
    """
    from decimal import Decimal

    from repositories.config_repository import ContextSearchConfigRepository

    config = await ContextSearchConfigRepository(db).create_or_get(ctx_id)
    config.reinforce_enabled = enabled
    config.reinforce_max_boost = Decimal(str(max_boost))
    await db.commit()


async def _score_reinforce_arm(
    svc: Any,
    corpus: Corpus,
    id_map: dict[str, str],
    owner: str,
    ctx_id: UUID,
    ws_id: UUID,
    adopted_docs: set[str],
) -> ArmBlock:
    """Recall every query (hybrid), group rankings by population, and compute the
    per-population metric blocks + the corpus-level zero-adoption surfacing rate.
    """
    from models.schemas import RecallRequest

    by_pop: dict[str, list[tuple[list[str], set[str]]]] = {p: [] for p in POPULATIONS}
    all_rankings: list[tuple[list[str], set[str]]] = []
    for q in corpus.queries:
        resp = await svc.recall(
            request=RecallRequest(query=q.text, k=_RECALL_K, search_mode="hybrid"),
            user_id=owner,
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )
        ranked_docs = [id_map.get(str(r.memory_id), "?") for r in resp.results]
        relevant = set(q.relevant)
        if q.population in by_pop:
            by_pop[q.population].append((ranked_docs, relevant))
        all_rankings.append((ranked_docs, relevant))

    return ArmBlock(
        populations={p: population_metrics(by_pop[p]) for p in POPULATIONS},
        zero_adoption_surfacing_rate=zero_adoption_surfacing_rate(
            all_rankings, adopted_docs, _RECALL_K
        ),
    )


async def _run_reinforce_arms(
    svc: Any,
    corpus: Corpus,
    id_map: dict[str, str],
    owner: str,
    ctx_id: UUID,
    ws_id: UUID,
    run_date: str,
    write: bool,
) -> dict[str, Any]:
    """Seed adoption, score the OFF arm, flip reinforce on, score the ON arm, and
    evaluate the rollout gate. Neural memory is forced OFF so the only thing that
    changes between arms is the reinforce re-rank itself.
    """
    # Fail fast on a malformed corpus: a typo'd / missing population would be
    # silently dropped from the per-population breakdown, shrinking the gate's
    # sample with no error. Every query must carry a recognized population and
    # both populations must be represented, or the A/B is not what it claims.
    by_population: dict[str, int] = dict.fromkeys(POPULATIONS, 0)
    for q in corpus.queries:
        if q.population not in by_population:
            raise RuntimeError(
                f"query {q.id!r} has population {q.population!r}; expected one of {POPULATIONS}"
            )
        by_population[q.population] += 1
    empty = [p for p, n in by_population.items() if n == 0]
    if empty:
        raise RuntimeError(f"reinforce corpus has no queries for population(s) {empty}")

    # Fail fast on an unresolvable adopted doc (#1084): silently dropping a typo'd
    # canonical id un-seeds its adoption and understates uplift with no error —
    # the asymmetry the population check above already guards against.
    rev: dict[str, UUID] = {doc_id: UUID(mid) for mid, doc_id in id_map.items()}
    declared_adopted = list(corpus.meta.get("adopted_docs", []))
    unresolved = [d for d in declared_adopted if d not in rev]
    if unresolved:
        raise RuntimeError(
            f"adopted_docs has unresolved doc id(s) {unresolved} — a typo here is "
            "silently un-seeded and understates the measured uplift; fix the corpus meta"
        )
    adopted_docs = set(declared_adopted)
    adopted_mem_ids = [rev[d] for d in sorted(adopted_docs)]

    prev_neural = os.environ.get("ENABLE_NEURAL_MEMORY")
    os.environ["ENABLE_NEURAL_MEMORY"] = "false"
    try:
        await _seed_adoption(svc, ctx_id, owner, adopted_mem_ids)

        # OFF arm: pin reinforce OFF explicitly. The recall path materializes
        # the config row via create_or_get (search_service._get_search_config),
        # and since #1207 a materialized row defaults to ENABLED — relying on
        # "no config row yet" would silently score the OFF arm with the
        # re-rank active.
        await _set_reinforce(svc.db, ctx_id, enabled=False, max_boost=_MAX_BOOST)
        off = await _score_reinforce_arm(svc, corpus, id_map, owner, ctx_id, ws_id, adopted_docs)

        # Flip reinforce ON for the same context + same seeded signal.
        await _set_reinforce(svc.db, ctx_id, enabled=True, max_boost=_MAX_BOOST)
        on = await _score_reinforce_arm(svc, corpus, id_map, owner, ctx_id, ws_id, adopted_docs)
    finally:
        if prev_neural is None:
            os.environ.pop("ENABLE_NEURAL_MEMORY", None)
        else:
            os.environ["ENABLE_NEURAL_MEMORY"] = prev_neural

    gate = evaluate_reinforce_gate(off, on, thresholds=GateThresholds())
    # Vacuous-pass guard (#1084): a neutered reinforce_enabled toggle (config not
    # read, or no adoption seeded) yields an ON arm identical to OFF, which still
    # PASSES the default gate (uplift 0 → improved False). Surfaced as a diagnostic
    # rather than raised — a legitimately no-headroom corpus (OFF already optimal)
    # can also tie. The rollout runbook graduates on gate.current_fact.improved,
    # NOT bare passed, so an identical/no-uplift run never graduates a context.
    off_on_arms_identical = asdict(off) == asdict(on)
    results: dict[str, Any] = {
        "run_date": run_date,
        "experiment": "reinforce_on_vs_off",
        "corpus_version": corpus.meta.get("version"),
        "query_count": len(corpus.queries),
        "doc_count": len(corpus.documents),
        "recall_k": _RECALL_K,
        "max_boost": _MAX_BOOST,
        "seed": {
            "references": _SEED_REFERENCES,
            "helpful_feedback": _SEED_HELPFUL,
            "adopted_docs": sorted(adopted_docs),
        },
        "off": asdict(off),
        "on": asdict(on),
        "off_on_arms_identical": off_on_arms_identical,
        "gate": gate,
    }

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = _RESULTS_DIR / f"reinforce-{run_date}.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"eval-reinforce: wrote {out}")
    return results


async def run_reinforce_eval(write: bool = True, run_date: str | None = None) -> dict[str, Any]:
    """Ingest the reinforce corpus, run the OFF/ON A/B, evaluate the gate."""
    from auth.workspace_roles import WorkspaceRole
    from db.base import get_db
    from models.auth import Context, Workspace, WorkspaceMember
    from services.memory_service import MemoryService
    from utils.datetime import utcnow

    corpus = _load_reinforce_corpus()
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
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
        await db.flush()
        await db.commit()

        svc = MemoryService(db)
        id_map: dict[str, str] = {}
        try:
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)
            return await _run_reinforce_arms(
                svc, corpus, id_map, owner, ctx.id, ws.id, run_date, write
            )
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))

    raise RuntimeError("database session unavailable — is the stack up?")


def _main() -> int:
    import asyncio

    results = asyncio.run(run_reinforce_eval())
    print(json.dumps(results, indent=2, ensure_ascii=False))
    gate = results["gate"]
    improved = gate["current_fact"]["improved"]
    # PASS alone is NOT a graduation signal (#1084): the default gate passes on
    # non-regression, so a no-op reinforce passes with improved=False. Graduate a
    # context only when reinforce actually HELPS (improved) — surface both.
    print(f"\nreinforce gate: {'PASS' if gate['passed'] else 'FAIL'}  (improved={improved})")
    for reason in gate["reasons"]:
        print(f"  - {reason}")
    if results.get("off_on_arms_identical"):
        print(
            "  ! OFF and ON arms are identical — reinforce had NO observable effect "
            "(neutered toggle / no seeded signal / no headroom). Do NOT graduate."
        )
    if gate["passed"] and not improved:
        print("  ! gate PASSED but reinforce did not improve current-fact — do NOT graduate.")
    # Non-zero exit on a failed gate so CI / an operator notices.
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
