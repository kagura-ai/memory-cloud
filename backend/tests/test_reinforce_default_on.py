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
   rewrite existing rows. Cohort truth: pre-#1207 contexts hold a stored
   ``false`` (rows are auto-stamped at context creation / by any past
   recall's ``create_or_get`` under the old default) and stay off; only the
   rare genuinely row-less legacy context adopts the new default lazily when
   its row is materialized — the recorded #1207 decision.
4. Partial-update semantics: ``ContextSearchConfigUpdate`` +
   ``model_dump(exclude_unset=True)`` (the repository's update contract)
   drops reinforce fields the caller did not send, so a partial PUT can never
   silently flip an explicit opt-out in either direction.
5. The reinforce guard's config lookup stays READ-ONLY (a fail-safe for
   genuinely row-less states) — pinned via source scan.
6. The eval harness pins reinforce OFF explicitly wherever an arm is not
   measuring the re-rank itself — the frozen retrieval/update/compounding
   protocols must stay byte-comparable across product default changes:
   the shared provisioning stamp (``_provisioning.py``), the update-slice
   vanilla arm (``update_runner.py``), the compounding harness's recall
   control lane (``replay_runner.py``), and the reinforce A/B OFF arm
   (``reinforce_runner.py``). Pinned via AST (constructor kwargs), not raw
   substrings, so a docstring mention can never satisfy the pin.
7. Recovery and reset semantics: admin context recovery pins
   ``reinforce_enabled=False`` (the lost row's setting is unknowable — recall
   ordering must not change as a side effect of disaster recovery), and
   ``reset_to_default`` passes the reinforce defaults explicitly (its
   ``update()`` applies ``exclude_unset``, so omission would leave stored
   values behind).
"""

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

from models.config import ContextSearchConfig
from models.schemas import ContextSearchConfigUpdate

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _column(name: str) -> Any:
    return ContextSearchConfig.__table__.c[name]


def _call_kwarg_values(path: Path, callee: str, kwarg: str) -> list[object]:
    """Return the literal value of ``kwarg`` for every ``callee(...)`` call.

    Calls that omit the kwarg contribute ``None`` (distinct from a literal
    ``None`` value, which does not occur for these boolean kwargs). Matching
    is on the call's function name (``Name`` or attribute tail), so docstring
    or comment mentions can never satisfy these pins.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: list[object] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != callee:
            continue
        value: object = None
        for kw in node.keywords:
            if kw.arg == kwarg and isinstance(kw.value, ast.Constant):
                value = kw.value.value
        values.append(value)
    return values


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
    """The migration changes the DDL default and never UPDATEs existing rows.

    ``op.execute`` is banned outright in this migration — the only legitimate
    operation is ``alter_column``. That closes the raw-SQL/sa.update()/aliased
    UPDATE loopholes a substring check on "update context_search_configs"
    would miss.
    """
    versions = _BACKEND_ROOT / "alembic" / "versions"
    matches = sorted(versions.glob("e55_1207_*.py"))
    assert matches, "expected alembic migration e55_1207_* for the default flip"
    source = matches[0].read_text(encoding="utf-8")
    assert "alter_column" in source
    assert "context_search_configs" in source
    assert "reinforce_enabled" in source
    assert "op.execute" not in source, (
        "e55 must not execute arbitrary SQL — a row rewrite would clobber "
        "stored opt-outs (#1207 recorded decision: DDL default change only)"
    )
    assert "sa.update" not in source, (
        "migration must not rewrite existing rows (#1207 recorded decision)"
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
    - ``update_runner.py``: the vanilla-RAG arm pins False, the MC arm True;
    - ``replay_runner.py``: the compounding harness's recall control lane
      pins False at provisioning;
    - ``reinforce_runner.py``: the A/B OFF arm disables explicitly before
      scoring (``_set_reinforce(..., enabled=False)``).

    AST-based on constructor/call kwargs — a docstring or comment containing
    the same text can never satisfy these pins.
    """
    eval_root = _BACKEND_ROOT / "tests" / "eval"

    prov = _call_kwarg_values(
        eval_root / "_provisioning.py", "ContextSearchConfig", "reinforce_enabled"
    )
    assert prov == [False], f"_provisioning stamp must pin False, got {prov}"

    upd = _call_kwarg_values(
        eval_root / "update_runner.py", "ContextSearchConfig", "reinforce_enabled"
    )
    assert sorted(upd, key=str) == [False, True], (
        f"update_runner must pin VR arm False and MC arm True explicitly, got {upd}"
    )

    replay = _call_kwarg_values(
        eval_root / "replay_runner.py", "ContextSearchConfig", "reinforce_enabled"
    )
    assert replay == [False], f"replay_runner control lane must pin False, got {replay}"

    reinforce = _call_kwarg_values(eval_root / "reinforce_runner.py", "_set_reinforce", "enabled")
    assert False in reinforce and True in reinforce, (
        f"reinforce_runner must set the OFF arm (enabled=False) and the ON arm "
        f"(enabled=True) explicitly, got {reinforce}"
    )


def test_recovery_and_reset_pin_reinforce_explicitly() -> None:
    """Recovery restores conservatively; reset converges to documented defaults.

    - Admin context recovery reconstructs the config row from Qdrant, where the
      lost row's reinforce setting is unknowable — it pins ``False`` so recall
      ordering never changes as a side effect of disaster recovery.
    - ``reset_to_default`` must pass the reinforce defaults explicitly: its
      ``update()`` applies ``exclude_unset``, so omitting the fields would
      silently leave stored values behind and "reset" would not mean defaults.
    """
    admin = _call_kwarg_values(
        _BACKEND_ROOT / "src" / "api" / "routes" / "admin.py",
        "ContextSearchConfig",
        "reinforce_enabled",
    )
    assert admin == [False], f"admin recovery must pin reinforce_enabled=False, got {admin}"

    reset = _call_kwarg_values(
        _BACKEND_ROOT / "src" / "repositories" / "config_repository.py",
        "ContextSearchConfigUpdate",
        "reinforce_enabled",
    )
    assert True in reset, (
        f"reset_to_default must pass reinforce_enabled=True explicitly, got {reset}"
    )
