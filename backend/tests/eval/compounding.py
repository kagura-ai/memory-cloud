"""Pure logic for the #969 compounding (cold→warm) retrieval experiment.

Tier B companion to the #967 multi-arm harness: #967 measures the *static*
quality of retrieval on a frozen corpus; this module plans and scores the
*compounding* experiment — does retrieval improve as the context is used?

Everything here is deterministic and infrastructure-free so it runs in normal
CI (``test_compounding.py``). The live cold → replay → warm orchestration that
actually drives ``recall()`` traffic lives in ``tests.eval.replay_runner``.

Design facts this protocol is built on (Issue #120, decision-pinned):

- ``recall()`` is the Hebbian **write** side — with neural memory enabled it
  co-activates the top results and strengthens graph edges, but its *ranking*
  reads only hybrid-search scores (usage-independent by design; mixing graph
  signals into recall degrades precision).
- ``explore()`` (activation spreading) is the **read** side — the surface
  where accumulated edges change retrieval outcomes.

So the experiment measures two lanes at every checkpoint:

- **graph lane** (primary): from a probe query's seed gold doc, can
  activation spreading recover the *companion* gold docs? Cold graphs cannot;
  a graph warmed by co-recall traffic should. This is where compounding lives.
- **recall lane** (control): P@5/MRR/nDCG of plain hybrid recall on the same
  queries. Expected flat — which is itself the guarantee that warming the
  graph never degrades the precision lane.

The corpus is held fixed throughout, so any warm lift is attributable to the
learned layer (growth ≠ "more data").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.eval.tools.corpus import Corpus

#: Replay traffic never includes the probe queries — the probes are strictly
#: held out, so warm lift measures *generalization* from related traffic.
MODE_EXCLUDE_PROBES = "exclude_probes"
#: Replay traffic includes every query (probes too) — models the production
#: reality that users re-ask the questions that matter to them (rehearsal).
MODE_INCLUDE_PROBES = "include_probes"

REPLAY_MODES = (MODE_EXCLUDE_PROBES, MODE_INCLUDE_PROBES)

#: Keys that appear in metric blocks but are not lift-comparable metrics.
_NON_METRIC_KEYS = frozenset({"n"})


@dataclass(frozen=True)
class ProbeSpec:
    """One held-out compounding probe: a multi-gold query, split into the
    seed doc (the retrieval entry point) and the companion docs the graph
    lane must recover from it."""

    query_id: str
    text: str
    bucket: str
    seed_doc: str
    companion_docs: tuple[str, ...]


@dataclass(frozen=True)
class ReplayPlan:
    """Deterministic plan for one cold→replay→warm experiment run."""

    mode: str
    rounds: int
    probes: tuple[ProbeSpec, ...]
    replay_query_ids: tuple[str, ...]


def build_replay_plan(
    corpus: Corpus, mode: str = MODE_EXCLUDE_PROBES, rounds: int = 8
) -> ReplayPlan:
    """Split the corpus queries into held-out probes and replay traffic.

    Probes are exactly the queries with >= 2 gold docs (fixture order): only
    multi-doc gold sets can show companion recovery, the graph-lane metric.
    Replay traffic is every other query (``exclude_probes``) or every query
    (``include_probes``), repeated ``rounds`` times by the live runner.
    """
    if mode not in REPLAY_MODES:
        raise ValueError(f"unknown replay mode {mode!r}; expected one of {REPLAY_MODES}")
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")

    probes = tuple(
        ProbeSpec(
            query_id=q.id,
            text=q.text,
            bucket=q.bucket,
            seed_doc=q.relevant[0],
            companion_docs=tuple(q.relevant[1:]),
        )
        for q in corpus.queries
        if len(q.relevant) >= 2
    )
    if not probes:
        raise ValueError("corpus has no multi-gold queries — nothing to probe")

    probe_ids = {p.query_id for p in probes}
    if mode == MODE_INCLUDE_PROBES:
        replay_ids = tuple(q.id for q in corpus.queries)
    else:
        replay_ids = tuple(q.id for q in corpus.queries if q.id not in probe_ids)
    if not replay_ids:
        raise ValueError("replay workload is empty — no traffic to warm the graph with")

    return ReplayPlan(mode=mode, rounds=rounds, probes=probes, replay_query_ids=replay_ids)


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Share of ``relevant`` ids present in the top-``k`` of ``ranked``.

    The graph-lane "companion recovery" metric. An empty gold set is a
    protocol bug (0/0), not a zero score — fail loud.
    """
    if not relevant:
        raise ValueError("relevant set must not be empty")
    hits = sum(1 for doc in ranked[:k] if doc in relevant)
    return hits / len(relevant)


@dataclass(frozen=True)
class PairAudit:
    """One co-activated pair observation from the replay workload, classified
    against the two write-path gates that decide whether a Hebbian edge can
    ever form (the 2026-06-10 live run showed these gates, not the mechanism,
    explain a zero lift on this corpus):

    - semantic gate: pair cosine must reach ``min_similarity_for_edge``
      (recall() skips the gate entirely when an embedding is missing);
    - prune cliff: a first update below ``prune_threshold`` is deleted, not
      stored, so sub-threshold pairs never accumulate across rounds.
    """

    query_id: str
    doc_a: str
    doc_b: str
    cosine: float | None
    delta_w: float
    verdict: str
    is_probe_gold_pair: bool


def classify_pair(
    cosine: float | None, delta_w: float, *, min_similarity: float, prune_threshold: float
) -> str:
    """Classify a co-activated pair against the edge-formation gates.

    Returns ``forms``, ``gated_cosine``, ``below_prune``, or
    ``gated_cosine+below_prune``. A ``None`` cosine mirrors recall()'s
    behaviour for missing embeddings: the semantic gate is skipped.
    """
    gates = []
    if cosine is not None and cosine < min_similarity:
        gates.append("gated_cosine")
    if delta_w < prune_threshold:
        gates.append("below_prune")
    return "+".join(gates) if gates else "forms"


def summarize_gate_audit(pairs: list[PairAudit]) -> dict[str, Any]:
    """Aggregate a gate audit: verdict counts + the probe-gold-pair detail.

    The probe gold pairs are the compounding-critical subset — every one that
    fails a gate directly explains a zero graph-lane lift.

    Also reports the **noise side** of edge formation (#982 / Gate1): lowering
    the semantic gate to admit genuine cross-topic gold pairs must not blow up
    spurious edges between unrelated (non-gold) pairs. ``edge_precision`` is the
    fraction of *formed* edges that are gold; ``non_gold_form_rate`` is the
    fraction of *non-gold* pairs that formed an edge (the false-edge rate the
    #118 gate exists to suppress). Pairing this with the recovery@k recall
    metric is what proves a recalibration helped rather than just added noise.
    """
    verdicts: dict[str, int] = {}
    formed_gold = 0
    formed_non_gold = 0
    non_gold_pair_count = 0
    for pair in pairs:
        verdicts[pair.verdict] = verdicts.get(pair.verdict, 0) + 1
        formed = pair.verdict == "forms"
        if pair.is_probe_gold_pair:
            if formed:
                formed_gold += 1
        else:
            non_gold_pair_count += 1
            if formed:
                formed_non_gold += 1

    formed_total = formed_gold + formed_non_gold
    return {
        "pair_observations": len(pairs),
        "verdicts": verdicts,
        # Recall side: did gold companion pairs form edges?
        "formed_total": formed_total,
        "formed_gold": formed_gold,
        # Noise side: did unrelated pairs form edges? (#982 / Gate1 guard)
        "formed_non_gold": formed_non_gold,
        "non_gold_pair_count": non_gold_pair_count,
        # precision of the formed-edge set; None when no edge formed (no lie).
        "edge_precision": (formed_gold / formed_total) if formed_total else None,
        # false-edge rate; None when there were no non-gold pairs to form from.
        "non_gold_form_rate": (
            formed_non_gold / non_gold_pair_count if non_gold_pair_count else None
        ),
        "probe_gold_pairs": [
            {
                "query_id": p.query_id,
                "pair": [p.doc_a, p.doc_b],
                "cosine": p.cosine,
                "delta_w": p.delta_w,
                "verdict": p.verdict,
            }
            for p in pairs
            if p.is_probe_gold_pair
        ],
    }


def compute_lift(cold: dict[str, Any], warm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-metric cold→warm lift table.

    Compares every numeric metric key shared by both blocks; ``abs`` is the
    absolute delta, ``rel`` the relative delta (``None`` when the cold
    baseline is 0.0 — undefined, not infinite). Non-numeric values and
    counters (``n``) are ignored. Diverging metric *sets* between the two
    blocks mean the checkpoints were measured differently — a protocol bug.
    """

    def metric_keys(block: dict[str, Any]) -> set[str]:
        return {
            key
            for key, value in block.items()
            if key not in _NON_METRIC_KEYS and isinstance(value, (int, float))
        }

    cold_keys, warm_keys = metric_keys(cold), metric_keys(warm)
    if cold_keys != warm_keys:
        raise ValueError(
            f"cold/warm metric keys diverge: only-cold={sorted(cold_keys - warm_keys)}, "
            f"only-warm={sorted(warm_keys - cold_keys)}"
        )

    lift: dict[str, dict[str, Any]] = {}
    for key in sorted(cold_keys):
        c, w = float(cold[key]), float(warm[key])
        lift[key] = {
            "cold": round(c, 4),
            "warm": round(w, 4),
            "abs": round(w - c, 4),
            "rel": round((w - c) / c, 4) if c != 0.0 else None,
        }
    return lift
