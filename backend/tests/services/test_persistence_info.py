"""Durability transparency on write responses (#1505).

Three things are tested, and the split matters:

1. The *contract* — what persistence_info() says for each scope, and for each
   of the three consolidation configurations (sleep / legacy / neither).
2. The *truth of the numbers* — the advertised archival floor is checked against
   the code that actually enforces it (``_archival_eligible`` for the sleep
   path, the ``age_days`` comparison for the legacy path), so the response
   cannot drift into promising a floor nobody enforces.
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
from datetime import datetime
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
    consolidation_archive_min_age_days,
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
    assert consolidation_archive_min_age_days() is None


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
    assert info.consolidation_archive_min_age_days == consolidation_archive_min_age_days()


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
    without touching delivery_mode). The floor sentence and the pinned sentence must
    not contradict each other: no "only forget() removes it" next to an archival floor."""
    info = persistence_info("working", pinned=True)
    assert info is not None
    assert "stays pinned" in info.detail
    assert "near-duplicate merge" in info.detail
    assert "only an explicit" not in info.detail


def test_pinned_no_pass_still_mentions_exemption(no_pass):
    info = persistence_info("working", pinned=True)
    assert info is not None
    assert "stays pinned" in info.detail


def test_unknown_scope_is_omitted_rather_than_raised():
    """Advisory field: an unexpected scope must not fail an already-committed write."""
    assert persistence_info("archived") is None
    assert persistence_info("") is None


# ---------------------------------------------------------------------------
# The advertised number is the enforced number
# ---------------------------------------------------------------------------


def test_advertised_floor_matches_the_sleep_path_archival_gate(sleep_pass):
    """One day under the advertised floor must be archival-INELIGIBLE.

    This is the anti-drift test: it exercises the predicate consolidation
    actually calls, so retuning ARCHIVE_MIN_AGE_DAYS without updating the
    response (or vice versa) fails here.
    """
    from services.sleep.consolidation import _archival_eligible

    days = consolidation_archive_min_age_days()
    assert days is not None
    memory = MagicMock(reference_count=0, created_at=datetime(2020, 1, 1))  # noqa: DTZ001
    cutoff = datetime(2019, 1, 1)  # noqa: DTZ001 — operator opt-in, already elapsed

    assert _archival_eligible(memory, days - 1, cutoff) is False, (
        "response promises nothing is archived before this age, but the sleep path would archive it"
    )
    assert _archival_eligible(memory, days, cutoff) is True, (
        "advertised floor is more conservative than the enforced one — it is stale"
    )


def test_legacy_floor_matches_the_legacy_constant(legacy_pass):
    from tasks.neural_tasks import LEGACY_ARCHIVE_MIN_AGE_DAYS

    assert consolidation_archive_min_age_days() == LEGACY_ARCHIVE_MIN_AGE_DAYS


def test_legacy_consolidation_compares_against_the_named_constant():
    """The legacy constant must be load-bearing, not decorative.

    consolidation_archive_min_age_days() derives from LEGACY_ARCHIVE_MIN_AGE_DAYS.
    If consolidation_task went back to comparing a bare literal, that derivation
    would report a number nothing reads — so assert the comparison itself.
    """
    import tasks.neural_tasks as neural_tasks

    tree = ast.parse(Path(neural_tasks.__file__).read_text(encoding="utf-8"))
    task = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "consolidation_task"
    )
    guarded = [
        cmp
        for cmp in ast.walk(task)
        if isinstance(cmp, ast.Compare)
        and isinstance(cmp.left, ast.Name)
        and cmp.left.id == "age_days"
        and any(
            isinstance(c, ast.Name) and c.id == "LEGACY_ARCHIVE_MIN_AGE_DAYS"
            for c in cmp.comparators
        )
    ]
    assert guarded, "consolidation_task no longer gates archival on LEGACY_ARCHIVE_MIN_AGE_DAYS"


# ---------------------------------------------------------------------------
# The limits of the claim (review finding: dedup ignores the floor)
# ---------------------------------------------------------------------------


def test_dedup_merge_still_has_no_age_or_adoption_gate():
    """Pin WHY the age field is named for consolidation only.

    ``DedupMergePhase._fetch_active_memories`` selects on identity columns,
    ``deleted_at`` and the two lane exemptions (#1519 pinned, #1524 time) alone
    — no age, scope, or reference_count gate — so an unpinned, non-time memory
    written minutes ago can lose a near-duplicate merge the same night. Any global "not removed before N days"
    wording would therefore be false.

    If a gate is ever added here, this test fails: revisit the response wording,
    because the promise could then legitimately be widened.
    """
    import services.sleep.dedup_merge as dedup_merge

    tree = ast.parse(Path(dedup_merge.__file__).read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fetch_active_memories"
    )
    referenced = {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Memory"
    }
    assert referenced == {
        "user_id",
        "deleted_at",
        "updated_at",
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
    # call, not an inline column — an exemption, not an age/adoption gate; the
    # working-scope floor wording stays as is and persistence_info(pinned=True)
    # carries the widened promise for pinned writes.
    assert _calls_not_pinned_predicate(fn)


def _calls_not_pinned_predicate(fn: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "not_pinned_predicate"
        for node in ast.walk(fn)
    )


def _async_fn(module, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


@pytest.mark.parametrize(
    ("module_name", "fn_name"),
    [
        ("services.sleep.dedup_merge", "_fetch_active_memories"),
        ("services.sleep.consolidation", "_fetch_working_memories"),
        ("services.sleep.importance_reeval", "_fetch_candidates"),
    ],
)
def test_every_automated_deleter_uses_the_shared_pinned_exemption(module_name, fn_name):
    """#1523: ``persistence_info(pinned=True)`` promises that Sleep maintenance does
    not select a pinned row. That promise is only as wide as the set of candidate
    fetches carrying ``not_pinned_predicate()`` — pin the set here so a new phase
    (or a hand-copied inline predicate) cannot silently narrow it."""
    import importlib

    module = importlib.import_module(module_name)
    assert _calls_not_pinned_predicate(_async_fn(module, fn_name)), (
        f"{module_name}.{fn_name} no longer calls not_pinned_predicate(); either the "
        "pinned promise in services/persistence.py is now false, or the exemption was "
        "re-spelled inline — use the shared predicate"
    )


@pytest.mark.parametrize("scope", ["working", "persistent"])
def test_response_makes_no_unscoped_retention_promise(sleep_pass, scope):
    """The detail text must not read as a retention SLA for the whole system.

    Both scopes need the caveat: ``_fetch_active_memories`` has no scope filter
    either, so "consolidation will not archive this one" is equally misleading
    on a persistent memory if left unqualified.
    """
    detail = persistence_info(scope).detail
    assert "merge" in detail and "forget()" in detail, (
        f"{scope} detail must name the lifecycles the consolidation floor does NOT bind"
    )


def test_dedup_merge_is_not_scope_gated():
    """Pin the reason the persistent branch needs the caveat too."""
    import services.sleep.dedup_merge as dedup_merge

    tree = ast.parse(Path(dedup_merge.__file__).read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fetch_active_memories"
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "scope" for node in ast.walk(fn)
    ), "dedup selection is now scope-gated — the persistent-scope wording can be widened"


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
        patch("services.quota_service.QuotaService"),
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
    assert (
        result.persistence.consolidation_archive_min_age_days
        == consolidation_archive_min_age_days()
    )


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
    assert (
        result.persistence.consolidation_archive_min_age_days
        == consolidation_archive_min_age_days()
    )


@pytest.mark.asyncio
async def test_upsert_populates_persistence(service, sleep_pass):
    """update_memory(external_id=...) — the create/replace path."""
    from models.schemas import UpdateMemoryRequest

    service.memory_repo.get_by_resource_id = AsyncMock(return_value=None)
    remembered = MagicMock()
    remembered.memory_id = uuid4()
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

    service.memory_repo.get_by_resource_id = AsyncMock(return_value=None)
    remembered = MagicMock()
    remembered.memory_id = uuid4()
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
    service.memory_repo.get_by_resource_id = AsyncMock(return_value=existing)
    remembered = MagicMock()
    remembered.memory_id = uuid4()
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
    service.memory_repo.get_by_resource_id = AsyncMock(return_value=existing)
    remembered = MagicMock()
    remembered.memory_id = uuid4()
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
    assert payload["consolidation_archive_min_age_days"] == consolidation_archive_min_age_days()
    assert payload["detail"]


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
        patch("mcp_server.tools.memory._check_viewer_permission", new=AsyncMock(return_value=None)),
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
    assert (
        payload["persistence"]["consolidation_archive_min_age_days"]
        == consolidation_archive_min_age_days()
    )


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
            persistence=persistence_info("persistent"),
            supersede_candidate_dismissed=None,  # #1504, not under test here
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

    persistence_info reads the archival floor through a lazy import of
    services.sleep.consolidation (Qdrant + graph service + LLM service). If that
    chain raises, an unguarded call would surface as a failed remember() for a
    memory that is already stored — and the handler logs memory_creation_failed
    and re-raises, inviting a retry that duplicates the memory.
    """

    def test_a_failing_floor_lookup_omits_the_block_instead_of_raising(self, sleep_pass):
        with patch(
            "services.persistence._sleep_archive_min_age_days",
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
