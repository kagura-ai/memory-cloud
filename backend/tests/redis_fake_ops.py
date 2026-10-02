"""Shared additions for the hand-written Redis fakes that back a real SessionManager.

Several tests model Redis with a tiny dict-backed class, so the JSON round
trip of a session record is real. Since #1809 SessionManager also keeps a
per-user index set and writes through pipelines (``SET XX``, ``SADD``,
``SREM``, ``SMEMBERS``). ``SessionFakeOps`` adds exactly those calls on top of
a fake that stores strings in ``self.store``; ``DeferredPipeline`` replays the
queued calls against the fake on ``execute`` — the order and return values a
real MULTI/EXEC gives.
"""

from __future__ import annotations

from typing import Any


class DeferredPipeline:
    """Queue calls, run them against the fake on ``execute()``."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def queue(*args: Any, **kwargs: Any) -> DeferredPipeline:
            self._ops.append((name, args, kwargs))
            return self

        return queue

    def execute(self, raise_on_error: bool = True) -> list[Any]:
        ops, self._ops = self._ops, []
        results: list[Any] = []
        for name, args, kwargs in ops:
            try:
                results.append(getattr(self._redis, name)(*args, **kwargs))
            except Exception as exc:
                if raise_on_error:
                    raise
                results.append(exc)
        return results


class SessionFakeOps:
    """Set and conditional-SET support for a fake whose strings live in ``self.store``."""

    store: dict[str, Any]

    @property
    def sets(self) -> dict[str, set[str]]:
        if not hasattr(self, "_sets"):
            self._sets: dict[str, set[str]] = {}
        return self._sets

    def set(
        self,
        key: str,
        value: Any,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool | None:
        exists = key in self.store
        if (nx and exists) or (xx and not exists):
            return None
        if ex is not None and hasattr(self, "setex"):
            self.setex(key, ex, value)
        else:
            self.store[key] = value
        return True

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        added = len(set(members) - bucket)
        bucket.update(members)
        return added

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key, set())
        removed = len(bucket & set(members))
        bucket.difference_update(members)
        return removed

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def expire(self, key: str, _ttl: int) -> bool:
        return key in self.store or key in self.sets

    def scan(self, cursor: int, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        return 0, [k for k in list(self.store) if k.startswith(prefix)]

    def pipeline(self, transaction: bool = True) -> DeferredPipeline:
        return DeferredPipeline(self)
