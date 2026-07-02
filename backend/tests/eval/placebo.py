"""Pure logic for the Day-2 placebo kill-shot (directional de-risk).

Infrastructure-free and deterministic — runs in normal CI (test_placebo.py).
The live cold→warm→placebo orchestration lives in tests.eval.placebo_runner.

Realizes the descriptive half of prereg-v1 H2: three arms (real-warm,
density-matched random-edge placebo, shuffled-gold placebo) scored on companion
recovery, compared by paired point estimates with DESCRIPTIVE bootstrap
intervals. No inferential claim, no gated CI — that is the Day-3 confirmatory
run at the single committed τ.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from statistics import median
from typing import Callable


@dataclass(frozen=True)
class Edge:
    """One neural-memory edge, reduced to what the null model needs. The
    non-endpoint attributes travel with the edge through a rewire (only src/dst
    change), so the placebo differs from real-warm in exactly which endpoints
    are connected — density, degree sequence and attribute multisets are held."""

    src: str
    dst: str
    weight: float
    origin: str
    confidence: float
    edge_type: str


def degree_preserving_rewire(edges: list[Edge], *, seed: int, swap_factor: int = 10) -> list[Edge]:
    """Maslov-Sneppen directed double-edge-swap null graph.

    Repeatedly picks two edges (s1->d1),(s2->d2) and swaps their destinations to
    (s1->d2),(s2->d1) — preserving every node's out-degree (src) and in-degree
    (dst), the edge count, and each edge's own weight/origin/confidence/edge_type
    (hence the exact attribute multisets). Rejects any swap that would create a
    self-loop or a parallel edge. Attempts ``swap_factor * len(edges)`` accepted
    swaps for mixing. Raises if fewer than two edges (nothing to swap).
    """
    if len(edges) < 2:
        raise ValueError(f"need >= 2 edges to rewire, got {len(edges)}")

    rng = random.Random(seed)
    current = list(edges)
    present = {(e.src, e.dst) for e in current}
    target = swap_factor * len(current)
    done = 0
    attempts = 0
    max_attempts = target * 20
    while done < target and attempts < max_attempts:
        attempts += 1
        i = rng.randrange(len(current))
        j = rng.randrange(len(current))
        if i == j:
            continue
        e1, e2 = current[i], current[j]
        new1 = (e1.src, e2.dst)
        new2 = (e2.src, e1.dst)
        if new1[0] == new1[1] or new2[0] == new2[1]:
            continue  # self-loop
        if new1 == new2 or new1 in present or new2 in present:
            continue  # parallel edge
        present.discard((e1.src, e1.dst))
        present.discard((e2.src, e2.dst))
        present.add(new1)
        present.add(new2)
        current[i] = replace(e1, dst=e2.dst)
        current[j] = replace(e2, dst=e1.dst)
        done += 1
    return current


def _derangement(n: int, rng: random.Random) -> list[int]:
    """A permutation of range(n) with no fixed point (n >= 2)."""
    while True:
        perm = list(range(n))
        rng.shuffle(perm)
        if all(perm[i] != i for i in range(n)):
            return perm


def permute_gold(probes, *, seed: int) -> dict[str, tuple[str, ...]]:
    """Permute which companion set each probe seed is scored against.

    Preserves each probe's companion-set SIZE. Probes are grouped by companion
    count; groups of >= 2 are deranged among themselves (no probe keeps its own
    gold). A singleton size-class draws its companions from the global gold-doc
    pool (all probes' seeds + companions) excluding the probe's own docs, so it
    is still "not own gold". Deterministic under ``seed``.
    """
    rng = random.Random(seed)
    groups: dict[int, list[int]] = defaultdict(list)
    for idx, p in enumerate(probes):
        groups[len(p.companion_docs)].append(idx)

    all_gold = sorted({d for p in probes for d in p.companion_docs} | {p.seed_doc for p in probes})

    result: dict[str, tuple[str, ...]] = {}
    for size, idxs in groups.items():
        if len(idxs) >= 2:
            originals = [probes[i].companion_docs for i in idxs]
            perm = _derangement(len(idxs), rng)
            for pos, i in enumerate(idxs):
                result[probes[i].query_id] = originals[perm[pos]]
        else:
            i = idxs[0]
            own = set(probes[i].companion_docs) | {probes[i].seed_doc}
            pool = [d for d in all_gold if d not in own]
            if len(pool) < size:
                raise ValueError(
                    f"cannot build a size-{size} foreign gold set for probe "
                    f"{probes[i].query_id!r}: only {len(pool)} non-own gold docs "
                    f"available — corpus too small for a size-preserving "
                    f"shuffled-gold placebo for this probe"
                )
            result[probes[i].query_id] = tuple(rng.sample(pool, size))
    return result
