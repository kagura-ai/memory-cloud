"""Day-3 corpus-freeze τ computation (prereg-v1 §3) — live, run ONCE per freeze.

Computes the FROZEN τ for the inferential H2 kill-shot: the median cosine over
the **held-out** cross-topic gold pairs of the frozen corpus, measured with the
rig's real embedder. Procedure (prereg-v1 §3, stamped into Appendix A):

    1. ingest the frozen corpus into a throwaway workspace (production write path)
    2. fetch the stored vectors for every corpus doc
    3. enumerate gold pairs from HELD-OUT multi-gold probes only (``split ==
       "heldout"``), keep the cross-topic ones (differing ``Document.source`` —
       the same definition the Day-2 directional probe used)
    4. τ := median pairwise cosine over that population
    5. teardown (corpus data is ephemeral; only the JSON survives)

Also records the counts Appendix A stamps (N_heldout, N_probe, doc/query counts),
the corpus ``content_sha256`` from its meta, the Sudachi version, and the
embedder identity — one self-contained freeze artifact. Real run only; numbers
are never fabricated. Heavy imports are function-local (module import is CI-safe).

Usage (inside the stack's api container):
    KAGURA_EVAL_LIVE=1 PYTHONPATH=src:. python -m tests.eval.freeze_tau \
        --corpus tests/eval/fixtures/kagura_l.yaml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.eval.placebo import median_cross_topic_gold_pair_cosine
from tests.eval.runner import _ingest_corpus, _sudachi_version, _teardown
from tests.eval.tools.corpus import Corpus, load_corpus

_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def heldout_cross_topic_gold_pairs(corpus: Corpus) -> set[frozenset[str]]:
    """Gold pairs from held-out multi-gold probes, cross-topic only.

    A probe is a query with >= 2 gold docs (same selection as
    ``build_replay_plan``) restricted to ``split == "heldout"``; a pair is
    cross-topic when its two docs have different ``Document.source``. Pure —
    unit-testable without the stack.
    """
    by_id = corpus.docs_by_id
    pairs: set[frozenset[str]] = set()
    for q in corpus.queries:
        if q.split != "heldout" or len(q.relevant) < 2:
            continue
        gold = list(q.relevant)
        for i in range(len(gold)):
            for j in range(i + 1, len(gold)):
                a, b = gold[i], gold[j]
                da, db = by_id.get(a), by_id.get(b)
                if da is None or db is None or da.source == db.source:
                    continue
                pairs.add(frozenset((a, b)))
    return pairs


async def run_freeze_tau(corpus_path: str, write: bool = True) -> dict[str, Any]:
    """Ingest the frozen corpus, measure τ per prereg-v1 §3, tear down."""
    from utils.datetime import utcnow

    corpus = load_corpus(Path(corpus_path))
    run_date = utcnow().strftime("%Y-%m-%d")

    n_heldout = sum(1 for q in corpus.queries if q.split == "heldout")
    n_public = sum(1 for q in corpus.queries if q.split == "public")
    heldout_probes = [q for q in corpus.queries if q.split == "heldout" and len(q.relevant) >= 2]
    # Valid H2 probes = held-out multi-gold whose gold set spans both sources
    # (the cross-topic population the compounding gate acts on).
    by_id = corpus.docs_by_id
    n_probe = sum(
        1
        for q in heldout_probes
        if {by_id[d].source for d in q.relevant if d in by_id} == {"memory", "resource"}
    )
    gold_pairs = heldout_cross_topic_gold_pairs(corpus)

    measurement = await _measure_tau(corpus, gold_pairs)

    results: dict[str, Any] = {
        "run_date": run_date,
        "experiment": "day3-corpus-freeze",
        "corpus_path": corpus_path,
        "corpus_version": corpus.meta.get("version"),
        "content_sha256": corpus.meta.get("content_sha256"),
        "sudachi_version": _sudachi_version(),
        "doc_count": len(corpus.documents),
        "query_count": len(corpus.queries),
        "n_heldout": n_heldout,
        "n_public": n_public,
        "n_probe": n_probe,
        "heldout_cross_topic_gold_pair_count": len(gold_pairs),
        "tau_method": "median_heldout_cross_topic_gold_pair_cosine (prereg-v1 §3)",
        **measurement,
        "design_note": (
            "Frozen τ for the inferential H2 kill-shot, computed ONCE at Day-3 "
            "corpus-freeze from the held-out cross-topic gold-pair cosine "
            "distribution — before any inferential result. Stamped into "
            "prereg-v1 Appendix A and tagged prereg-v1-frozen."
        ),
    }

    if write:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(corpus_path).stem
        out = _RESULTS_DIR / f"freeze-{stem}-{run_date}.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"freeze-tau: wrote {out}")
    return results


async def _measure_tau(corpus: Corpus, gold_pairs: set[frozenset[str]]) -> dict[str, Any]:
    """Provision, ingest, fetch vectors, compute τ, tear down (always)."""
    from db.base import get_db
    from db.qdrant import ensure_kagura_memories_collection, get_collection_name, get_qdrant_client
    from neural.utils import cosine_similarity
    from services.memory_service import MemoryService
    from tasks.neural_calibration import _load_measure_script
    from tests.eval._provisioning import provision_eval_context

    async for db in get_db():
        # Shared eval provisioning (#19): stamp the per-context embedding config
        # so ingest routes to the rig's real collection.
        owner, ws, ctx, emb_model, emb_dims = await provision_eval_context(db)

        svc = MemoryService(db)
        id_map: dict[str, str] = {}
        try:
            await ensure_kagura_memories_collection(
                emb_dims, get_collection_name(emb_model, emb_dims)
            )
            await _ingest_corpus(svc, corpus, owner, ctx.id, ws.id, id_map)

            script = _load_measure_script()
            model_name, dims, collection = await script.resolve_embedding_model(db, ctx.id)
            qdrant = get_qdrant_client()
            vecs_by_mem = await script.fetch_vectors(qdrant, collection, [UUID(m) for m in id_map])
            doc_vec = {id_map[m]: v for m, v in vecs_by_mem.items() if m in id_map}
            source_by_doc = {d.id: d.source for d in corpus.documents}

            tau = median_cross_topic_gold_pair_cosine(
                doc_vec, gold_pairs, source_by_doc, cosine_fn=cosine_similarity
            )
            pair_cosines = sorted(
                round(cosine_similarity(doc_vec[a], doc_vec[b]), 4)
                for pr in gold_pairs
                if len(pr) == 2
                for a, b in [tuple(pr)]
                if a in doc_vec and b in doc_vec
            )
            return {
                "embedding_model": model_name,
                "embedding_dimensions": dims,
                "vectors_fetched": len(doc_vec),
                "tau_frozen": round(tau, 4) if tau is not None else None,
                "pair_cosine_min": pair_cosines[0] if pair_cosines else None,
                "pair_cosine_max": pair_cosines[-1] if pair_cosines else None,
            }
        finally:
            await _teardown(svc, db, owner, ctx.id, ws.id, list(id_map.keys()))

    raise RuntimeError("database session unavailable — is the stack up?")


def _main() -> int:
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, help="corpus YAML path (the frozen fixture)")
    ap.add_argument("--no-write", action="store_true", help="print only, skip the JSON artifact")
    args = ap.parse_args()

    results = asyncio.run(run_freeze_tau(args.corpus, write=not args.no_write))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
