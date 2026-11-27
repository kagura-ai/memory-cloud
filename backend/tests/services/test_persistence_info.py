"""Durability transparency on write responses (#1505).

Three things are tested, and the split matters:

1. The *contract* — what persistence_info() says for each scope, and for each
   of the three consolidation configurations (sleep / legacy / neither).
2. The *truth of the rule* — #3021: a memory is kept until deleted. The
   archive-age field is always null, and the two consolidation passes are
   checked for having no delete left, so the response cannot drift into
   promising a keep that a pass breaks.
3. The *limits of the claim* — a first review of this change caught the response
   asserting "nothing you write today can be dropped", which is false: the
   near-duplicate merge pass soft-deletes at any age. The scoping is now part of
   the contract, so there is a test pinning why.

The wiring tests use the mocked-DB pattern from test_remember_delivery_mode.py:
a real MemoryService.remember() call, so a construction site that forgets the
field fails here rather than shipping an invisible feature.
"""

import ast
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import RememberRequest
from services.memory_service import MemoryService
from services.persistence import (
    LEGACY_CONSOLIDATION,
    SLEEP_CONSOLIDATION,
    active_consolidation_pass,
    persistence_info,
)


@pytest.fixture
def sleep_pass(monkeypatch):
    monkeypatch.setenv("SLEEP_ENABLED", "true")
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")


@pytest.fixture
def legacy_pass(monkeypatch):
    monkeypatch.setenv("SLEEP_ENABLED", "false")
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "false")


@pytest.fixture
def no_pass(monkeypatch):
    """SLEEP_ENABLED on but neural memory off: the sleep task returns at its
    guard and schedule_neural_tasks skips the legacy cron — nothing runs."""
    monkeypatch.setenv("SLEEP_ENABLED", "true")
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "false")


# ---------------------------------------------------------------------------
# Which pass is running
# ---------------------------------------------------------------------------


def test_sleep_config_reports_the_sleep_pass(sleep_pass):
    assert active_consolidation_pass() == SLEEP_CONSOLIDATION


def test_default_config_reports_the_legacy_pass(legacy_pass):
    assert active_consolidation_pass() == LEGACY_CONSOLIDATION


def test_sleep_without_neural_memory_reports_no_pass(no_pass):
    """The configuration where NEITHER pass runs must not advertise one."""
    assert active_consolidation_pass() is None


def test_env_is_read_per_call_not_frozen_at_import(monkeypatch):
    """A cached pass name would misreport a reconfigured process."""
    monkeypatch.setenv("SLEEP_ENABLED", "false")
    assert active_consolidation_pass() == LEGACY_CONSOLIDATION
    monkeypatch.setenv("SLEEP_ENABLED", "true")
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
    assert active_consolidation_pass() == SLEEP_CONSOLIDATION


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_working_scope_is_reported_as_already_committed(sleep_pass):
    """The whole point of #1505: 'working' must not read as 'not saved yet'."""
    info = persistence_info("working")
    assert info is not None
    assert info.scope == "working"
    assert info.committed is True
    assert info.promotes_via == SLEEP_CONSOLIDATION
    # #3021: no pass archives, so there is no age floor to advertise.
    assert info.consolidation_archive_min_age_days is None


def test_legacy_config_advertises_the_legacy_pass_and_its_criteria(legacy_pass):
    """Promotion criteria differ per pass; the legacy one has no centrality rule."""
    info = persistence_info("working")
    assert info is not None
    assert info.promotes_via == LEGACY_CONSOLIDATION
    assert "centrality" not in info.detail
    assert "reference()" not in info.detail


def test_sleep_config_advertises_adoption_and_centrality(sleep_pass):
    info = persistence_info("working")
    assert info is not None
    assert "reference()" in info.detail
    assert "centrality" in info.detail


def test_no_pass_advertises_neither_promotion_nor_a_floor(no_pass):
    info = persistence_info("working")
    assert info is not None
    assert info.promotes_via is None
    assert info.consolidation_archive_min_age_days is None


def test_persistent_scope_reports_no_removal_floor(sleep_pass):
    """Consolidation selects on scope == 'working', so persistent has no floor."""
    info = persistence_info("persistent")
    assert info is not None
    assert info.scope == "persistent"
    assert info.committed is True
    assert info.promotes_via is None
    assert info.consolidation_archive_min_age_days is None


def test_pinned_write_reports_merge_exemption(sleep_pass):
    """#1519: pinned (delivery_mode='always') memories never enter dedup
    candidacy, so the merge caveat must not be repeated for them."""
    info = persistence_info("persistent", pinned=True)
    assert info is not None
    assert "near-duplicate merge" in info.detail
    assert "stays pinned" in info.detail  # hedged: the exemption holds while the pin holds
    assert "only an explicit" not in info.detail  # forget() is not the only removal path
    assert "still apply" not in info.detail  # the generic caveat is replaced, not appended


def test_unpinned_persistent_still_carries_the_merge_caveat(sleep_pass):
    info = persistence_info("persistent")
    assert info is not None
    assert "near-duplicate merge" in info.detail
    assert "pinned" not in info.detail


def test_pinned_working_wording_is_coherent(sleep_pass):
    """A pinned row CAN be scope='working' (rollback_sleep_run's _undo_promote demotes
    without touching delivery_mode). The pinned sentence must not claim that only
    forget() removes it (deleting the context does too)."""
    info = persistence_info("working", pinned=True)
    assert info is not None
    assert "stays pinned" in info.detail
    assert "near-duplicate merge" in info.detail
    assert "only an explicit" not in info.detail


def test_pinned_no_pass_still_mentions_exemption(no_pass):
    info = persistence_info("working", pinned=True)
    assert info is not None
    assert "stays pinned" in info.detail


@pytest.mark.parametrize("scope", ["persistent", "working"])
def test_guardrail_write_reports_the_maintenance_exemption(sleep_pass, scope):
    """A tool guardrail (details.tool_trigger) is excluded from dedup and
    importance re-evaluation like a pinned row, so the
    generic "merge still applies" caveat would be false for it."""
    info = persistence_info(scope, tool_triggered=True)
    assert info is not None
    assert "details.tool_trigger" in info.detail
    assert "near-duplicate merge" in info.detail
    assert "leaves its importance alone" in info.detail
    assert "still apply" not in info.detail
    assert "only an explicit" not in info.detail
    assert "pinned" not in info.detail  # it is not pinned; do not claim so
    assert "demotes" not in info.detail  # rollback may still demote a promoted guardrail


def test_pinned_guardrail_names_both_holds(sleep_pass):
    info = persistence_info("persistent", pinned=True, tool_triggered=True)
    assert info is not None
    assert "stays pinned" in info.detail
    assert "details.tool_trigger" in info.detail
    assert "never demotes it" in info.detail


def test_guardrail_no_pass_still_mentions_exemption(no_pass):
    info = persistence_info("working", tool_triggered=True)
    assert info is not None
    assert "details.tool_trigger" in info.detail


def test_unmarked_write_does_not_mention_guardrails(sleep_pass):
    info = persistence_info("working")
    assert info is not None
    assert "tool_trigger" not in info.detail


def test_unknown_scope_is_omitted_rather_than_raised():
    """Advisory field: an unexpected scope must not fail an already-committed write."""
    assert persistence_info("archived") is None
    assert persistence_info("") is None


# ---------------------------------------------------------------------------
# The advertised rule is the enforced rule (#3021: kept until deleted)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["sleep_pass", "legacy_pass", "no_pass"])
@pytest.mark.parametrize("scope", ["working", "persistent"])
@pytest.mark.parametrize("pinned", [False, True])
def test_every_block_says_kept_until_deleted_and_has_no_floor(request, fixture, scope, pinned):
    request.getfixturevalue(fixture)
    info = persistence_info(scope, pinned=pinned)
    assert info is not None
    assert info.consolidation_archive_min_age_days is None
    assert "kept until deleted" in info.detail
    assert "archive" not in info.detail
    assert "floor" not in info.detail


def _calls_named(fn: ast.AST, names: set[str]) -> set[str]:
    """The attribute calls in ``fn`` whose method name is in ``names``."""
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    }


_DELETES = {"delete", "soft_delete", "_queue_forgotten_points", "delete_forgotten_points"}


def test_the_legacy_consolidation_task_deletes_nothing():
    """The rule the legacy pass's reply states: it promotes, it never deletes."""
    import tasks.neural_tasks as neural_tasks

    assert _calls_named(_async_fn(neural_tasks, "consolidation_task"), _DELETES) == set()


def test_sleep_consolidation_deletes_nothing():
    """The same for the Sleep pass: no statement in the phase removes a row."""
    import services.sleep.consolidation as consolidation

    tree = ast.parse(Path(consolidation.__file__).read_text(encoding="utf-8"))
    assert _calls_named(tree, _DELETES | {"queue_point_deletes"}) == set()
    assert "DELETED_BY_SLEEP_ARCHIVE" not in Path(consolidation.__file__).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The limits of the claim (review finding: dedup ignores the floor)
# ---------------------------------------------------------------------------


# #3019: the one eligibility rule of dedup, and the three reads built on it.
_DEDUP_RULE = "dedup_eligible"
_DEDUP_RULE_CALLERS = ("_fetch_active_memories", "_eligible_outside_ids", "_load_cluster_members")
# What the callers may name besides the rule: the id, the embedding status of
# a neighbour (its stored vector must match its text) and its point id.
_DEDUP_CALLER_COLUMNS = {"id", "embedding_status", "summary_embedding_id"}
# The two lane statements (services/sleep/coverage.py) order and bound the
# window and keep each lane's position; the two lanes together read every row
# (the changed lane what changed since its position, the sweep every row on
# each pass), so neither is an age gate.
_DEDUP_LANE_COLUMNS = {"updated_at", "created_at", "id"}


def _memory_attributes(fn: ast.AST) -> set[str]:
    return {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Memory"
    }


def _calls_name(fn: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(fn)
    )


def test_dedup_merge_still_has_no_age_or_adoption_gate():
    """Pin WHY the detail names the merge next to forget().

    ``dedup_eligible`` — the one rule for the rows dedup reads, compares as
    neighbours and merges (#3019) — selects on identity columns,
    ``deleted_at`` and the lane exemptions (#1519 pinned, #1524 time, tool
    guardrails, #2996 resource-owned) alone — no age, scope, or
    reference_count gate — so an unpinned, non-time memory written minutes ago
    can lose a near-duplicate merge the same night, and a memory of any age can
    lose one to a newly written duplicate. Any global "not removed before N
    days" wording would therefore be false.

    If a gate is ever added here, this test fails: revisit the response wording,
    because the promise could then legitimately be widened.
    """
    import services.sleep.dedup_merge as dedup_merge

    rule = _fn(dedup_merge, _DEDUP_RULE)
    referenced = _memory_attributes(rule)
    assert referenced == {
        "user_id",
        "deleted_at",
        "workspace_id",
        "context_id",
        # #1524: time memories (type='time') are the recall_upcoming lane and are
        # excluded the same way — a lane exemption, still not an age gate.
        "type",
    }, (
        f"dedup selection columns changed to {sorted(referenced)} — if an age or "
        "adoption gate was added, the persistence wording can be widened"
    )
    # #1519/#1523: the pinned exemption is the shared ``not_pinned_predicate()``
    # call, not an inline column — an exemption, not an age/adoption gate;
    # persistence_info(pinned=True) carries the widened promise for pinned writes.
    assert _calls_not_pinned_predicate(rule)

    # Every read of dedup builds its WHERE from the rule and adds no gate.
    for caller in _DEDUP_RULE_CALLERS:
        fn = _fn(dedup_merge, caller)
        assert _calls_name(fn, _DEDUP_RULE), f"{caller} does not apply {_DEDUP_RULE}"
        extra = _memory_attributes(fn) - _DEDUP_CALLER_COLUMNS
        assert not extra, f"{caller} names {sorted(extra)} beside {_DEDUP_RULE}"

    # #3019: the window's two lanes order and bound the rows the rule allows.
    import services.sleep.coverage as coverage

    lanes = _fn(coverage, "fetch_window")
    assert _memory_attributes(lanes) == _DEDUP_LANE_COLUMNS


def _is_predicate_call(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _calls_predicate(fn: ast.AST, name: str) -> bool:
    """True when ``<name>()`` is an ARGUMENT of some call in ``fn`` — a
    ``.where(...)`` clause or an ``only_if=`` keyword — never a stray statement
    or an unused local, which would leave the query unguarded while the name is
    still present in the body."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if any(_is_predicate_call(arg, name) for arg in node.args):
            return True
        if any(_is_predicate_call(kw.value, name) for kw in node.keywords):
            return True
    return False


def _calls_not_pinned_predicate(fn: ast.AST) -> bool:
    return _calls_predicate(fn, "not_pinned_predicate")


def _fn(module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The function ``name`` of ``module``, sync or async (#3019: the shared
    dedup rule is a plain function)."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


_async_fn = _fn


@pytest.mark.parametrize(
    ("module_name", "fn_name"),
    [
        # candidate fetches (read side). #3019: dedup's window fetch, neighbour
        # check and member load all apply this one rule (pinned above).
        ("services.sleep.dedup_merge", "dedup_eligible"),
        ("services.sleep.consolidation", "_fetch_working_memories"),
        ("services.sleep.importance_reeval", "_fetch_candidates"),
        # writes that follow an unlocked fetch (a pin can land in between)
        ("services.sleep.importance_reeval", "execute"),
        # rollback writes
        ("mcp_server.tools.sleep", "_undo_promote"),
        ("mcp_server.tools.sleep", "_undo_update_importance"),
    ],
)
def test_every_automated_deleter_uses_the_shared_pinned_exemption(module_name, fn_name):
    """#1523: ``persistence_info(pinned=True)`` promises that Sleep maintenance does
    not select, merge, re-score or demote a pinned row. That promise is only as
    wide as the set of queries carrying ``not_pinned_predicate()`` as a WHERE /
    ``only_if`` argument — pin the set here so a new phase (or a hand-copied
    inline predicate) cannot silently narrow it."""
    import importlib

    module = importlib.import_module(module_name)
    assert _calls_not_pinned_predicate(_async_fn(module, fn_name)), (
        f"{module_name}.{fn_name} no longer passes not_pinned_predicate() into its "
        "query; either the pinned promise in services/persistence.py is now false, or "
        "the exemption was re-spelled inline — use the shared predicate"
    )


@pytest.mark.parametrize(
    ("module_name", "fn_name"),
    [
        # candidate fetches (read side). #3019: dedup's window fetch, neighbour
        # check and member load all apply this one rule (pinned above).
        ("services.sleep.dedup_merge", "dedup_eligible"),
        ("services.sleep.consolidation", "_fetch_working_memories"),
        ("services.sleep.importance_reeval", "_fetch_candidates"),
        # writes that follow an unlocked fetch (a marking can land in between)
        ("services.sleep.importance_reeval", "execute"),
        # rollback write — a guardrail keeps the importance its author set.
        # (_undo_promote is deliberately absent: a guardrail is not scope-bound,
        # so a demote is harmless to the lane.)
        ("mcp_server.tools.sleep", "_undo_update_importance"),
    ],
)
def test_every_automated_deleter_uses_the_shared_tool_trigger_exemption(module_name, fn_name):
    """``persistence_info(tool_triggered=True)`` promises that Sleep maintenance
    does not select, merge or re-score a tool guardrail, and that rollback
    leaves its importance alone. Pin the set of queries carrying
    ``not_tool_triggered_predicate()`` exactly as the pinned twin above does."""
    import importlib

    module = importlib.import_module(module_name)
    assert _calls_predicate(_async_fn(module, fn_name), "not_tool_triggered_predicate"), (
        f"{module_name}.{fn_name} no longer passes not_tool_triggered_predicate() into "
        "its query; either the guardrail promise in services/persistence.py is now "
        "false, or the exemption was re-spelled inline — use the shared predicate"
    )


def test_pinned_working_rows_are_repaired_by_migration():
    """#1523: the consolidation exemption removes pinned working rows from the
    promote branch too, so the migration that moves them to persistent must ship
    with it (pin-on-write is the only remaining producer, and it is persistent)."""
    src = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "e78_1523_repin_scope.py"
    text = src.read_text(encoding="utf-8")
    assert "delivery_mode = 'always'" in text and "scope = 'working'" in text
    assert "SET scope = 'persistent'" in text


@pytest.mark.parametrize("scope", ["working", "persistent"])
def test_response_makes_no_unscoped_retention_promise(sleep_pass, scope):
    """The detail text must not read as a retention SLA for the whole system.

    Both scopes need the caveat: ``_fetch_active_memories`` has no scope filter
    either, so "kept until deleted" is equally misleading on a persistent memory
    if left without the removals that remain.
    """
    detail = persistence_info(scope).detail
    assert "merge" in detail and "forget()" in detail, (
        f"{scope} detail must name the removals that remain"
    )


def test_dedup_merge_is_not_scope_gated():
    """Pin the reason the persistent branch needs the caveat too."""
    import services.sleep.coverage as coverage
    import services.sleep.dedup_merge as dedup_merge

    for module, name in (
        *((dedup_merge, fn_name) for fn_name in (_DEDUP_RULE, *_DEDUP_RULE_CALLERS)),
        (coverage, "fetch_window"),
    ):
        fn = _fn(module, name)
        assert not any(
            isinstance(node, ast.Attribute) and node.attr == "scope" for node in ast.walk(fn)
        ), f"dedup {name} is now scope-gated — the persistent-scope wording can be widened"


# ---------------------------------------------------------------------------
# Wiring: the block reaches an actual write response
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()

    svc = MemoryService(db)
    context = MagicMock()
    context.id = uuid4()
    context.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(context, str(context.workspace_id), str(context.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc._create_declared_links = AsyncMock()
    svc._mock_context = context
    return svc


async def _remember(service, **kwargs):
    request = RememberRequest(
        summary="durability contract surfaced on write",
        content="see #1505",
        type="note",
        **kwargs,
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        # #1549: the quota gates run against the context's workspace even with
        # current_workspace_id=None, so the stub must be awaitable and permissive.
        patch(
            "services.quota_service.QuotaService",
            **{
                "return_value.check_memory_quota": AsyncMock(return_value=(True, None)),
                "return_value.check_memories_per_day": AsyncMock(return_value=(True, None)),
            },
        ),
    ):
        return await service.remember(
            request,
            user_id="test_user",
            client="test",
            current_context_id=service._mock_context.id,
            current_workspace_id=None,
        )


@pytest.mark.asyncio
async def test_remember_response_carries_the_persistence_block(service, sleep_pass):
    result = await _remember(service)
    assert result.scope == "working"
    assert result.persistence is not None, "remember() dropped the #1505 block"
    assert result.persistence.scope == "working"
    assert result.persistence.committed is True
    assert result.persistence.consolidation_archive_min_age_days is None


@pytest.mark.asyncio
async def test_pinned_write_reports_persistent_persistence(service, sleep_pass):
    """delivery_mode='always' pins to persistent — the block must follow the real scope."""
    result = await _remember(service, delivery_mode="always")
    assert result.scope == "persistent"
    assert result.persistence is not None
    assert result.persistence.scope == "persistent"
    assert result.persistence.consolidation_archive_min_age_days is None
    # #1519: the pinned flag must reach the builder through the real write path.
    assert "pinned" in result.persistence.detail


# The update paths need their OWN service-level coverage. An earlier revision
# tested them only by handing a PersistenceInfo to a mocked service and
# asserting the handler splatted it back — which passes even when the service
# never populates the field. Both construction sites shipped it missing, and the
# whole suite stayed green. These drive the real methods.


@pytest.mark.asyncio
async def test_in_place_update_populates_persistence(service, sleep_pass):
    """update_memory(memory_id=...) — the in-place path."""
    from models.schemas import UpdateMemoryRequest

    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "Original summary for testing"
    memory.context_summary = None
    memory.content = "Original content"
    memory.details = None
    memory.type = "note"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    memory.created_at = None
    memory.updated_at = None
    memory.deleted_at = None
    memory.embedding_status = "success"
    service.memory_repo.get = AsyncMock(return_value=memory)

    with (
        patch("services.permission_service.PermissionService") as perm_cls,
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
    ):
        perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)
        result = await service._update_in_place(
            UpdateMemoryRequest(memory_id=memory.id, importance=0.9),
            user_id="test_user",
        )

    assert result.persistence is not None, "_update_in_place dropped the #1505 block"
    assert result.persistence.scope == "working"
    assert result.persistence.consolidation_archive_min_age_days is None


@pytest.mark.asyncio
async def test_upsert_populates_persistence(service, sleep_pass):
    """update_memory(external_id=...) — the create/replace path."""
    from models.schemas import UpdateMemoryRequest

    service.memory_repo.list_by_resource_id = AsyncMock(return_value=[])
    remembered = MagicMock()
    remembered.memory_id = uuid4()
    remembered.observed_at = None  # #2689: RememberResponse's is a str or None
    # The upsert carries the event dates of the new memory as well.
    for _event_field in (
        "event_start",
        "event_end",
        "event_phrase",
        "event_weekday",
        "event_unresolved",
    ):
        setattr(remembered, _event_field, None)
    remembered.scope = "working"
    remembered.persistence = persistence_info("working")
    service.remember = AsyncMock(return_value=remembered)

    result = await service._upsert_by_external_id(
        UpdateMemoryRequest(
            external_id="new-resource",
            summary="Brand new memory for the upsert path",
            content="content",
            type="note",
        ),
        user_id="test_user",
        client="mcp",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    assert result.operation == "created"
    assert result.persistence is not None, "_upsert_by_external_id dropped the #1505 block"
    assert result.persistence.scope == "working"


@pytest.mark.asyncio
async def test_upsert_forwards_delivery_mode_and_reuses_remember_persistence(service, sleep_pass):
    """#1519: the upsert path delegates to remember(); the caller's pin must reach it
    (otherwise a pinned external_id row is replaced by an unpinned one), and the
    persistence block remember() built — with the pinned flag — is the one returned."""
    from models.schemas import UpdateMemoryRequest

    service.memory_repo.list_by_resource_id = AsyncMock(return_value=[])
    remembered = MagicMock()
    remembered.memory_id = uuid4()
    remembered.observed_at = None  # #2689: RememberResponse's is a str or None
    # The upsert carries the event dates of the new memory as well.
    for _event_field in (
        "event_start",
        "event_end",
        "event_phrase",
        "event_weekday",
        "event_unresolved",
    ):
        setattr(remembered, _event_field, None)
    remembered.scope = "persistent"
    remembered.persistence = persistence_info("persistent", pinned=True)
    service.remember = AsyncMock(return_value=remembered)

    result = await service._upsert_by_external_id(
        UpdateMemoryRequest(
            external_id="pinned-resource",
            summary="Pinned memory via the upsert path",
            content="content",
            type="note",
            delivery_mode="always",
        ),
        user_id="test_user",
    )

    forwarded = service.remember.await_args.args[0]
    assert forwarded.delivery_mode == "always"
    assert result.persistence is remembered.persistence
    assert "stays pinned" in result.persistence.detail


@pytest.mark.asyncio
async def test_upsert_replacement_inherits_existing_pin_when_omitted(service, sleep_pass):
    """#1519 (Copilot review on PR #1522): UpdateMemoryRequest.delivery_mode=None
    means "unchanged". Replacing a pinned external_id row without restating the
    pin must forward the existing row's 'always' — otherwise RememberRequest's
    default (on_recall) silently unpins it and forget() removes the pinned row."""
    from models.schemas import UpdateMemoryRequest

    existing = MagicMock()
    existing.id = uuid4()
    existing.delivery_mode = "always"
    service.memory_repo.list_by_resource_id = AsyncMock(return_value=[existing])
    remembered = MagicMock()
    remembered.memory_id = uuid4()
    remembered.observed_at = None  # #2689: RememberResponse's is a str or None
    # The upsert carries the event dates of the new memory as well.
    for _event_field in (
        "event_start",
        "event_end",
        "event_phrase",
        "event_weekday",
        "event_unresolved",
    ):
        setattr(remembered, _event_field, None)
    remembered.scope = "persistent"
    remembered.persistence = persistence_info("persistent", pinned=True)
    service.remember = AsyncMock(return_value=remembered)
    service.forget = AsyncMock()

    result = await service._upsert_by_external_id(
        UpdateMemoryRequest(
            external_id="pinned-resource",
            summary="Replacement body, pin not restated",
            content="content",
            type="note",
        ),
        user_id="test_user",
    )

    forwarded = service.remember.await_args.args[0]
    assert forwarded.delivery_mode == "always"
    assert result.operation == "replaced"


@pytest.mark.asyncio
async def test_upsert_explicit_delivery_mode_overrides_existing_pin(service, sleep_pass):
    """An explicit on_recall on the replacement is a deliberate unpin and wins
    over the existing row's 'always'."""
    from models.schemas import UpdateMemoryRequest

    existing = MagicMock()
    existing.id = uuid4()
    existing.delivery_mode = "always"
    service.memory_repo.list_by_resource_id = AsyncMock(return_value=[existing])
    remembered = MagicMock()
    remembered.memory_id = uuid4()
    remembered.observed_at = None  # #2689: RememberResponse's is a str or None
    # The upsert carries the event dates of the new memory as well.
    for _event_field in (
        "event_start",
        "event_end",
        "event_phrase",
        "event_weekday",
        "event_unresolved",
    ):
        setattr(remembered, _event_field, None)
    remembered.scope = "persistent"
    remembered.persistence = persistence_info("persistent")
    service.remember = AsyncMock(return_value=remembered)
    service.forget = AsyncMock()

    await service._upsert_by_external_id(
        UpdateMemoryRequest(
            external_id="pinned-resource",
            summary="Replacement that deliberately unpins",
            content="content",
            type="note",
            delivery_mode="on_recall",
        ),
        user_id="test_user",
    )

    forwarded = service.remember.await_args.args[0]
    assert forwarded.delivery_mode == "on_recall"


def test_every_write_response_construction_populates_persistence():
    """No construction site may omit the field.

    The two service-level tests above cover today's paths; this catches a NEW
    construction site added later without it — the shape of the regression that
    already happened once.
    """
    import services.memory_service as memory_service

    tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"RememberResponse", "UpdateMemoryResponse"}
    ]
    assert len(sites) >= 3, f"expected the known write-response sites, found {len(sites)}"
    for call in sites:
        assert any(kw.arg == "persistence" for kw in call.keywords), (
            f"{call.func.id} at line {call.lineno} is built without persistence — "
            "the block would be silently absent from that response"
        )


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_mcp_helper_emits_the_block_when_present(sleep_pass):
    from mcp_server.tools._helpers import _persistence_response_field

    field = _persistence_response_field(persistence_info("working"))
    assert set(field) == {"persistence"}
    payload = field["persistence"]
    assert payload["scope"] == "working"
    assert payload["committed"] is True
    assert payload["promotes_via"] == SLEEP_CONSOLIDATION
    assert payload["consolidation_archive_min_age_days"] is None
    # #1851: the prose rides along only on request; guide(["persistence"]) has it.
    assert "detail" not in payload
    verbose = _persistence_response_field(persistence_info("working"), verbose=True)
    assert verbose["persistence"]["detail"]


def test_mcp_helper_omits_the_key_when_scope_is_unclassifiable():
    """Absent beats null — the caller should not have to special-case a null block."""
    from mcp_server.tools._helpers import _persistence_response_field

    assert _persistence_response_field(None) == {}


@contextlib.contextmanager
def _patched_write_handler(service):
    """Mocked seams for a write handler call (mirrors test_context_last_used_touch)."""
    db = AsyncMock()

    async def mock_get_db():
        yield db

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=MagicMock())),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield


@pytest.mark.asyncio
async def test_remember_tool_json_carries_the_block(sleep_pass):
    """End of the wire: what the agent actually reads back from the MCP tool."""
    import json

    from mcp_server.tools.memory import handle_remember

    service = MagicMock()
    service.remember = AsyncMock(
        return_value=SimpleNamespace(
            memory_id=uuid4(),
            scope="working",
            version=1,  # #2043, not under test here
            persistence=persistence_info("working"),
            lint=[],  # #1502 hints, not under test here
        )
    )

    with _patched_write_handler(service):
        result = await handle_remember(
            {
                "summary": "durability contract surfaced on write",
                "content": "see #1505",
                "type": "note",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    assert payload["persistence"]["scope"] == "working"
    assert payload["persistence"]["committed"] is True
    assert payload["persistence"]["consolidation_archive_min_age_days"] is None


@pytest.mark.asyncio
async def test_update_memory_tool_json_carries_the_block(sleep_pass):
    import json

    from mcp_server.tools.memory import handle_update_memory

    service = MagicMock()
    service.update_memory = AsyncMock(
        return_value=SimpleNamespace(
            memory_id=uuid4(),
            operation="updated",
            re_embedded=False,
            scope="persistent",
            version=2,  # #2043, not under test here
            persistence=persistence_info("persistent"),
            supersede_candidate_dismissed=None,  # #1504, not under test here
            superseded_memory_id=None,  # #2308, not under test here
            lint=[],  # #1502, not under test here
        )
    )

    with _patched_write_handler(service):
        result = await handle_update_memory(
            {"memory_id": str(uuid4()), "summary": "x" * 20, "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    assert payload["persistence"]["scope"] == "persistent"
    assert payload["persistence"]["consolidation_archive_min_age_days"] is None


# ---------------------------------------------------------------------------
# Agent-facing text hygiene (#1417 convention)
# ---------------------------------------------------------------------------


def test_persistence_schema_text_carries_no_bare_issue_ids():
    """PersistenceInfo's docstring and Field descriptions ship to OpenAPI.

    Bare issue IDs are noise to an agent and leak internal provenance into a
    public API surface.
    """
    import re

    from models.schemas import PersistenceInfo

    texts = [PersistenceInfo.__doc__ or ""]
    texts += [f.description or "" for f in PersistenceInfo.model_fields.values()]
    for text in texts:
        assert not re.search(r"#\d{2,}", text), f"bare issue id in agent-facing text: {text!r}"


class TestPersistenceInfoCanNeverFailACommittedWrite:
    """Review finding: this runs inside remember()'s rollback try/except.

    If building the block raises, an unguarded call would surface as a failed
    remember() for a memory that is already stored — and the handler logs
    memory_creation_failed and re-raises, inviting a retry that duplicates the
    memory.
    """

    def test_a_failing_build_omits_the_block_instead_of_raising(self, sleep_pass):
        with patch(
            "services.persistence._merge_caveat",
            side_effect=ImportError("simulated broken import chain"),
        ):
            assert persistence_info("working") is None

    def test_a_failing_pass_lookup_omits_only_the_block_that_needs_it(self, sleep_pass):
        """The persistent branch never consults the pass, so it still builds."""
        with patch(
            "services.persistence.active_consolidation_pass",
            side_effect=RuntimeError("boom"),
        ):
            assert persistence_info("working") is None
            persistent = persistence_info("persistent")
            assert persistent is not None
            assert persistent.scope == "persistent"

    def test_remember_still_returns_when_the_block_cannot_be_built(self):
        """The write is committed; the response must still come back."""
        from services.persistence import persistence_info as real

        assert real("nonsense-scope") is None
