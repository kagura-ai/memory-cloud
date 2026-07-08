"""Regression tests for #1207: cold-start recency re-rank enabled by default.

The kagura-memory-eval program attributed the update-correctness headline
(+0.36 conditional lift over vanilla RAG, BCa 95% [0.24, 0.50]) entirely to
the bounded reinforce recency re-rank. #1207 flips the per-context default to
ON for newly created config rows so fresh contexts get the eval-proven update
kernel without discovering a flag.

Pins:

1. ORM defaults: ``ContextSearchConfig.reinforce_enabled`` has Python default
   ``True`` and ``server_default "true"`` — new rows (ORM inserts from
   ``context_service`` / ``resource setup`` / ``config_repository.create_or_get``
   and raw-SQL inserts alike) start enabled.
2. ``reinforce_max_boost`` stays at the eval-measured bound ``0.15`` — #1207
   changes the gate, not the magnitude.
3. Migration ``e55_1207_*`` alters only the column default; it must NOT
   rewrite existing rows (explicit opt-outs keep their stored value). The
   recorded #1207 decision for contexts without a config row is **lazy
   adoption**: the search path materializes the row via ``create_or_get`` on
   their next recall, so only a stored explicit ``false`` opts out.
4. Partial-update semantics: ``ContextSearchConfigUpdate`` +
   ``model_dump(exclude_unset=True)`` (the repository's update contract)
   drops reinforce fields the caller did not send, so a partial PUT can never
   silently flip an explicit opt-out in either direction.
5. The reinforce guard's config lookup stays READ-ONLY (a fail-safe for
   genuinely row-less states) — pinned via source scan.
6. The eval harness pins reinforce OFF explicitly wherever an arm is not
   measuring the re-rank itself — the frozen retrieval/update protocols must
   stay byte-comparable across product default changes: the shared
   provisioning stamp (``_provisioning.py``), the update-slice vanilla arm
   (``update_runner.py``), and the reinforce A/B OFF arm
   (``reinforce_runner.py``).
"""

from decimal import Decimal
from pathlib import Path

from models.config import ContextSearchConfig
from models.schemas import ContextSearchConfigUpdate

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _column(name: str):
    return ContextSearchConfig.__table__.c[name]


def test_reinforce_enabled_python_default_is_true() -> None:
    """New ORM rows (context creation paths omit the kwarg) start enabled."""
    col = _column("reinforce_enabled")
    assert col.default is not None, "reinforce_enabled must keep a Python-side default"
    assert col.default.arg is True


def test_reinforce_enabled_server_default_is_true() -> None:
    """Raw-SQL inserts get the same default as ORM inserts (DDL parity)."""
    col = _column("reinforce_enabled")
    assert col.server_default is not None
    assert str(col.server_default.arg) == "true"


def test_reinforce_max_boost_bound_unchanged() -> None:
    """#1207 flips the gate only — the eval-measured ±0.15 bound stays."""
    col = _column("reinforce_max_boost")
    assert col.default.arg == Decimal("0.15")
    assert str(col.server_default.arg) == "0.15"


def test_migration_e55_alters_default_without_row_rewrite() -> None:
    """The migration changes the DDL default and never UPDATEs existing rows."""
    versions = _BACKEND_ROOT / "alembic" / "versions"
    matches = sorted(versions.glob("e55_1207_*.py"))
    assert matches, "expected alembic migration e55_1207_* for the default flip"
    source = matches[0].read_text(encoding="utf-8")
    assert "alter_column" in source
    assert "context_search_configs" in source
    assert "reinforce_enabled" in source
    lowered = source.lower()
    assert "update context_search_configs" not in lowered, (
        "migration must not rewrite existing rows — explicit opt-outs and "
        "legacy contexts keep their stored value (#1207 recorded decision)"
    )


def test_partial_update_preserves_omitted_reinforce_fields() -> None:
    """A PUT that omits reinforce fields must not touch them.

    ``ContextSearchConfigRepository.update`` applies
    ``model_dump(exclude_unset=True)``; this pin fails if someone replaces
    that with a plain ``model_dump()`` or makes the reinforce fields required,
    either of which would let a partial update overwrite an explicit opt-out.
    """
    update = ContextSearchConfigUpdate(
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
    )
    dumped = update.model_dump(exclude_unset=True)
    assert "reinforce_enabled" not in dumped
    assert "reinforce_max_boost" not in dumped
    assert "reinforce_require_host_arbitration" not in dumped


def test_reinforce_guard_is_readonly_failsafe() -> None:
    """The re-rank's own config lookup stays read-only and fails closed.

    ``_maybe_reinforce_rerank`` must use ``get_by_context`` (never
    ``create_or_get``) and treat a missing row as OFF. In practice the search
    path has usually materialized the row earlier in the same recall — with
    the #1207 default — so this guard is a fail-safe for genuinely row-less
    states, not a legacy-context opt-out. Pinning it prevents a refactor from
    adding a write side effect or defaulting a missing row to ON here.
    """
    source = (_BACKEND_ROOT / "src" / "services" / "memory_service.py").read_text(encoding="utf-8")
    assert 'if cfg is None or not getattr(cfg, "reinforce_enabled", False):' in source
    assert "READ-ONLY lookup" in source


def test_eval_harness_pins_reinforce_off_explicitly() -> None:
    """Eval arms that are not measuring the re-rank pin it OFF explicitly.

    Since #1207 a lazily-materialized config row defaults to enabled, so any
    eval context relying on "default off" or "no config row yet" would
    silently score with the re-rank active, breaking comparability with the
    frozen protocols and archived results:

    - ``_provisioning.py``: the shared retrieval-eval stamp (golden runner,
      placebo runner, freeze_tau) pins ``reinforce_enabled=False``;
    - ``update_runner.py``: the vanilla-RAG arm pins it False (the MC arm
      sets True explicitly);
    - ``reinforce_runner.py``: the A/B OFF arm disables it explicitly before
      scoring.
    """
    eval_root = _BACKEND_ROOT / "tests" / "eval"

    provisioning = (eval_root / "_provisioning.py").read_text(encoding="utf-8")
    assert "reinforce_enabled=False" in provisioning

    update_runner = (eval_root / "update_runner.py").read_text(encoding="utf-8")
    assert "reinforce_enabled=False" in update_runner
    assert "reinforce_enabled=True" in update_runner  # MC arm stays explicit

    reinforce_runner = (eval_root / "reinforce_runner.py").read_text(encoding="utf-8")
    assert "_set_reinforce(svc.db, ctx_id, enabled=False" in reinforce_runner
