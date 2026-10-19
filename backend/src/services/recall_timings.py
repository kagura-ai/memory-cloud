"""Per-stage clock behind the ``recall_completed`` log line (#1989).

``MemoryService.recall`` creates one ``RecallTimings`` per request and hands
it to ``SearchService.hybrid_search`` as an out-parameter (the same three-hop
shape as the #1515 ``degradation`` dict). Each stage is a ``with`` block; a
stage entered twice accumulates. ``log_fields()`` renders integer
milliseconds, omits the stages that did not run (``rerank_ms`` absent means
"no rerank", not "fast"), and reports the unmeasured remainder as
``other_ms`` so ``duration_ms`` can be reconciled against the stage sum.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

STAGES: tuple[str, ...] = (
    "embed",
    "qdrant",
    "fulltext",
    "rerank",
    "hydrate",
    "select",
    "finalize",
    "commit",
    "post_commit",
)
"""Stage names in log order; every ``<stage>_ms`` field comes from this list."""


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


@dataclass
class RecallTimings:
    """Wall-clock seconds per recall stage, from one injectable clock.

    Attributes:
        clock: Monotonic clock; tests pass a fake to make durations exact.
        embed_cache_hit: Whether the query embedding came from the cache
            (``None`` until the embed stage ran).
    """

    clock: Callable[[], float] = time.perf_counter
    embed_cache_hit: bool | None = None
    _started: float = field(init=False, repr=False)
    _stages: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time the block as ``name``; an exception inside is still timed."""
        started = self.clock()
        try:
            yield
        finally:
            self._stages[name] = self._stages.get(name, 0.0) + (self.clock() - started)

    def discard(self, name: str) -> None:
        """Forget a stage that turned out not to run (its time goes to ``other_ms``)."""
        self._stages.pop(name, None)

    def log_fields(self, **context: Any) -> dict[str, Any]:
        """Structured fields for ``recall_completed``, in a stable order.

        Args:
            **context: Size context appended after the timings (``results``,
                ``candidates``, ``k``, ``context_count``).

        Returns:
            ``duration_ms`` (start to now), one ``<stage>_ms`` per stage that
            ran, ``other_ms`` (duration minus the measured stages, floored at
            0), ``embed_cache_hit`` when known, then ``context``.
        """
        duration = self.clock() - self._started
        fields: dict[str, Any] = {"duration_ms": _ms(duration)}
        for name in STAGES:
            if name in self._stages:
                fields[f"{name}_ms"] = _ms(self._stages[name])
        fields["other_ms"] = _ms(max(duration - sum(self._stages.values()), 0.0))
        if self.embed_cache_hit is not None:
            fields["embed_cache_hit"] = self.embed_cache_hit
        fields.update(context)
        return fields
