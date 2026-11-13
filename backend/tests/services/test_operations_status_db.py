"""Operations status against Postgres (Issue #2768).

- the lane snapshot and the probe writes on ``operations_llm_lanes``;
- the lane set the leader derives (judge pair from ``neural_config``);
- the ledger usage per lane: platform-paid calls of the lane's provider and
  model only, the probe's own rows never counted;
- the backlogs match the seeded tables;
- the probe never resolves a workspace's own key (a seeded key is present);
- the real route response: jobs with overdue / failed, and no identifier of
  a user, workspace or context, no address and no message.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.settings import Settings
from models.auth import (
    Context,
    EmailActionToken,
    EmailSignupRequest,
    ExternalAPIKey,
    User,
    UserKnownDevice,
    Workspace,
    WorkspaceMember,
    WorkspaceOwnershipForceTransferRequest,
    WorkspaceRole,
)
from models.deferred_point_delete import DeferredPointDelete
from models.erasure import ErasureRequest
from models.file_objects import FileObject, StorageObjectDeletion
from models.llm_call_log import LLMCallLog
from models.memory import Memory
from models.operations_status import OperationsLlmLane, ScheduledJobStatus
from models.sleep import SleepReport, SleepReportLLMUsage
from models.vector_erasure import VectorErasureJob
from services.operations import job_status
from services.operations.backlogs import read_backlogs
from services.operations.llm_lanes import (
    LaneSnapshot,
    configured_platform_lanes,
    lane_usage,
    write_lane_snapshot,
)
from services.operations.probe import (
    ProbeClients,
    ProbeResult,
    ProbeTarget,
    ledger_probe,
    record_probe,
    run_probe,
)
from services.operations.status import read_operations_status

NOW = datetime.now(UTC).replace(microsecond=0)
OLD = datetime(2001, 1, 2, 3, 4, 5)  # naive UTC, older than anything else in the DB
SEED_SIGNATURE = "Seed.Test.Signature-565"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


@pytest.fixture
async def factory(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(job_status, "_session_factory", maker)

    async def _clean() -> None:
        async with async_engine.begin() as conn:
            await conn.execute(text("DELETE FROM operations_llm_lanes"))
            await conn.execute(text("DELETE FROM scheduled_job_status"))
            await conn.execute(
                text("DELETE FROM llm_call_log WHERE call_metadata->>'stage' = 'operations_probe'")
            )

    await _clean()
    yield maker
    await _clean()


@pytest.fixture
def fresh_judge_config():
    from neural.config import NeuralMemoryConfig

    NeuralMemoryConfig._db_cache.clear()
    yield
    NeuralMemoryConfig._db_cache.clear()


def _snap(lane: str, provider: str, model: str, kind: str = "completion") -> LaneSnapshot:
    return LaneSnapshot(
        lane=lane,  # type: ignore[arg-type]
        provider=provider,
        model=model,
        credential_present=True,
        probe_kind=kind,  # type: ignore[arg-type]
    )


async def _lanes(maker) -> dict[str, OperationsLlmLane]:
    async with maker() as session:
        rows = (await session.execute(select(OperationsLlmLane))).scalars().all()
        return {r.lane: r for r in rows}


def _result(target: ProbeTarget, ok: bool, at: datetime, error: str | None = None) -> ProbeResult:
    return ProbeResult(target=target, ok=ok, latency_ms=42, error_class=error, at=at)


# ---------------------------------------------------------------------------
# Snapshot and probe writes
# ---------------------------------------------------------------------------


class TestLaneSnapshot:
    async def test_write_replace_and_reset(self, factory) -> None:
        await write_lane_snapshot(
            [_snap("embedding", "openai", "e1", "embedding"), _snap("sleep_judge", "openai", "m1")]
        )
        rows = await _lanes(factory)
        assert set(rows) == {"embedding", "sleep_judge"}
        assert rows["sleep_judge"].credential_present is True
        assert rows["sleep_judge"].probe_consecutive_failures == 0

        target = ProbeTarget("completion", "openai", "m1", ("sleep_judge",))
        await record_probe(_result(target, False, NOW, "AuthenticationError"))
        await record_probe(_result(target, False, NOW + timedelta(seconds=1), "TimeoutError"))
        row = (await _lanes(factory))["sleep_judge"]
        assert (row.last_probe_outcome, row.probe_consecutive_failures) == ("failed", 2)
        assert row.last_probe_error_class == "TimeoutError"
        assert row.last_probe_success_at is None

        # An older result landing late changes nothing.
        await record_probe(_result(target, True, NOW - timedelta(minutes=5)))
        assert (await _lanes(factory))["sleep_judge"].probe_consecutive_failures == 2

        await record_probe(_result(target, True, NOW + timedelta(seconds=2)))
        row = (await _lanes(factory))["sleep_judge"]
        assert (row.last_probe_outcome, row.probe_consecutive_failures) == ("ok", 0)
        assert row.last_probe_success_at == NOW + timedelta(seconds=2)
        assert row.last_probe_latency_ms == 42

        # Same provider/model: the probe columns stay; a new model resets them;
        # a lane no longer configured goes.
        await write_lane_snapshot([_snap("sleep_judge", "openai", "m1")])
        rows = await _lanes(factory)
        assert set(rows) == {"sleep_judge"}
        assert rows["sleep_judge"].last_probe_outcome == "ok"
        await write_lane_snapshot([_snap("sleep_judge", "openai", "m2")])
        row = (await _lanes(factory))["sleep_judge"]
        assert row.model == "m2"
        assert (row.last_probe_at, row.last_probe_outcome, row.probe_consecutive_failures) == (
            None,
            None,
            0,
        )

    async def test_probe_of_an_outdated_target_does_not_touch_the_lane(self, factory) -> None:
        await write_lane_snapshot([_snap("analysis", "openai", "m2")])
        stale = ProbeTarget("completion", "openai", "m1", ("analysis",))
        await record_probe(_result(stale, False, NOW, "TimeoutError"))
        assert (await _lanes(factory))["analysis"].last_probe_at is None

    async def test_successful_probe_is_ledgered_without_identity(self, factory) -> None:
        from services.llm_providers import Usage

        target = ProbeTarget("completion", "openai", "m-probe-ledger", ("analysis",))
        result = ProbeResult(
            target=target,
            ok=True,
            latency_ms=5,
            error_class=None,
            at=NOW,
            completion_usage=Usage(total=3, input=2, output=1, cached=0),
        )
        await ledger_probe(result)
        async with factory() as session:
            row = (
                await session.execute(
                    select(LLMCallLog).where(LLMCallLog.model == "m-probe-ledger")
                )
            ).scalar_one()
        assert (row.caller, row.call_type, row.paid_by) == ("admin", "completion", "platform")
        assert (row.user_id, row.workspace_id, row.context_id) == (None, None, None)
        assert row.call_metadata is not None
        assert row.call_metadata["stage"] == "operations_probe"


# ---------------------------------------------------------------------------
# The lane set
# ---------------------------------------------------------------------------


class TestConfiguredLanes:
    async def test_lanes_from_settings_and_the_judge_pair(
        self, db_session, monkeypatch, fresh_judge_config
    ) -> None:
        monkeypatch.setenv("SLEEP_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("SLEEP_LLM_MODEL", "judge-model")
        monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "llm")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform")
        await db_session.execute(text("DELETE FROM neural_config WHERE key LIKE 'sleep_llm_%'"))
        settings = Settings(
            _env_file=None,
            embedding_model="text-embedding-3-small",
            managed_llm_provider="self_hosted",
            managed_llm_model="local-chat",
            self_hosted_base_url="http://inference.example.com",
            enable_reranking=True,
            rerank_base_url="http://rr.example.com",
            rerank_model="rr-small",
        )
        lanes = {s.lane: s for s in await configured_platform_lanes(db_session, settings)}
        assert list(lanes) == [
            "embedding",
            "sleep_judge",
            "supersede_verify",
            "recall_selector",
            "analysis",
            "rerank",
        ]
        assert (lanes["embedding"].provider, lanes["embedding"].credential_present) == (
            "openai",
            True,
        )
        judge = lanes["sleep_judge"]
        assert (judge.provider, judge.model, judge.credential_present) == (
            "anthropic",
            "judge-model",
            False,
        )
        assert (lanes["analysis"].provider, lanes["analysis"].model) == (
            "self_hosted",
            "local-chat",
        )
        assert lanes["analysis"].credential_present is True
        assert (lanes["rerank"].model, lanes["rerank"].probe_kind) == ("rr-small", "rerank")

    async def test_no_judge_no_judge_lanes(self, db_session, monkeypatch) -> None:
        from types import SimpleNamespace

        from neural.config import NeuralMemoryConfig

        async def _from_db(cls, db):
            return SimpleNamespace(
                sleep_llm_provider="", sleep_llm_model="", supersede_verify_mode="llm"
            )

        monkeypatch.setattr(NeuralMemoryConfig, "from_db", classmethod(_from_db))
        settings = Settings(_env_file=None, enable_reranking=False)
        lanes = [s.lane for s in await configured_platform_lanes(db_session, settings)]
        assert lanes == ["embedding"]


# ---------------------------------------------------------------------------
# Ledger usage
# ---------------------------------------------------------------------------


def _call(**values: Any) -> LLMCallLog:
    base: dict[str, Any] = {
        "occurred_at": NOW.replace(tzinfo=None) - timedelta(minutes=10),
        "caller": "recall",
        "call_type": "embedding",
        "provider": "openai",
        "model": "emb-usage-model",
        "paid_by": "platform",
        "user_id": "u-usage",
        "workspace_id": uuid4(),
        "context_id": uuid4(),
    }
    base.update(values)
    return LLMCallLog(**base)


class TestLaneUsage:
    async def test_embedding_counts_platform_rows_of_the_lane_only(self, db_session) -> None:
        naive = NOW.replace(tzinfo=None)
        db_session.add_all(
            [
                _call(),
                _call(occurred_at=naive - timedelta(hours=3)),
                _call(occurred_at=naive - timedelta(days=3)),
                _call(paid_by="byok"),
                _call(model="other-model"),
                _call(caller="admin", user_id=None, workspace_id=None, context_id=None),
            ]
        )
        await db_session.flush()
        usage = await lane_usage(
            db_session,
            "embedding",
            "openai",
            "emb-usage-model",
            now=NOW,
            resolve_stored_byok_keys=True,
        )
        assert (usage.recorded, usage.calls_last_hour, usage.calls_last_day) == (True, 1, 2)
        assert usage.last_success_at == NOW - timedelta(minutes=10)
        assert usage.attribution == "platform"

    async def test_selector_success_excludes_timeouts_and_failures(self, db_session) -> None:
        naive = NOW.replace(tzinfo=None)

        def sel(outcome: str, minutes: int) -> LLMCallLog:
            return _call(
                call_type="completion",
                model="sel-model",
                occurred_at=naive - timedelta(minutes=minutes),
                call_metadata={"stage": "llm_selector", "outcome": outcome},
            )

        db_session.add_all([sel("applied", 30), sel("timeout", 5), sel("failed", 1)])
        # The probe's own completion on the same model never matches a lane.
        db_session.add(
            _call(
                caller="admin",
                call_type="completion",
                model="sel-model",
                user_id=None,
                workspace_id=None,
                context_id=None,
                call_metadata={"stage": "operations_probe", "target": "completion"},
            )
        )
        await db_session.flush()
        usage = await lane_usage(
            db_session,
            "recall_selector",
            "openai",
            "sel-model",
            now=NOW,
            resolve_stored_byok_keys=True,
        )
        assert usage.calls_last_day == 3
        assert usage.last_success_at == NOW - timedelta(minutes=30)

    async def test_sleep_judge_reads_platform_sleep_reports(self, db_session) -> None:
        naive = NOW.replace(tzinfo=None)
        ok_report = SleepReport(
            id=uuid4(), user_id="u-sleep", started_at=naive - timedelta(hours=2)
        )
        failed_report = SleepReport(
            id=uuid4(), user_id="u-sleep", status="failed", started_at=naive - timedelta(minutes=30)
        )
        byok_report = SleepReport(id=uuid4(), user_id="u-sleep", paid_by="byok", started_at=naive)
        db_session.add_all([ok_report, failed_report, byok_report])
        await db_session.flush()

        def usage_row(
            report: SleepReport, created: datetime, calls: int = 2
        ) -> SleepReportLLMUsage:
            return SleepReportLLMUsage(
                report_id=report.id,
                phase="edge_discovery",
                provider="openai",
                model="judge-usage-model",
                input_tokens=10,
                output_tokens=5,
                calls=calls,
                created_at=created,
            )

        db_session.add_all(
            [
                usage_row(ok_report, naive - timedelta(hours=2)),
                usage_row(failed_report, naive - timedelta(minutes=20)),
                usage_row(byok_report, naive - timedelta(minutes=1)),
            ]
        )
        await db_session.flush()
        usage = await lane_usage(
            db_session,
            "sleep_judge",
            "openai",
            "judge-usage-model",
            now=NOW,
            resolve_stored_byok_keys=True,
        )
        assert usage.attribution == "includes_workspace_keys"
        assert usage.last_success_at == NOW - timedelta(hours=2)
        assert (usage.calls_last_hour, usage.calls_last_day) == (2, 4)

        plain = await lane_usage(
            db_session,
            "sleep_judge",
            "openai",
            "judge-usage-model",
            now=NOW,
            resolve_stored_byok_keys=False,
        )
        assert plain.attribution == "platform"

    async def test_rerank_is_not_ledgered(self, db_session) -> None:
        usage = await lane_usage(
            db_session, "rerank", "self_hosted", "rr", now=NOW, resolve_stored_byok_keys=True
        )
        assert (usage.recorded, usage.calls_last_day, usage.last_success_at) == (False, None, None)


# ---------------------------------------------------------------------------
# Backlogs
# ---------------------------------------------------------------------------


async def _scope(db: AsyncSession, tag: str) -> tuple[str, Workspace, Context]:
    owner = f"ops-{tag}-{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@example.com", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"ops-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ops-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return owner, ws, ctx


def _memory(owner: str, ctx: Context, **values: Any) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        user_id=owner,
        summary="operations status backlog seed",
        content="c",
        type="note",
        client="pytest",
        scope="working",
        **values,
    )


async def _seed_backlogs(db: AsyncSession) -> tuple[str, Workspace, Context, dict[str, int]]:
    """One or two items per queue, each queue's oldest at ``OLD``; returns the expected deltas."""
    from config.constants import MAX_EMBEDDING_RETRIES

    owner, ws, ctx = await _scope(db, "backlog")
    aware_old = OLD.replace(tzinfo=UTC)
    db.add_all(
        [
            _memory(owner, ctx, embedding_status="pending", created_at=OLD),
            _memory(owner, ctx, embedding_status="pending", created_at=OLD + timedelta(days=1)),
            _memory(owner, ctx, embedding_status="processing", created_at=OLD),
            _memory(owner, ctx, embedding_status="failed", embedding_retry_count=0, created_at=OLD),
            _memory(
                owner,
                ctx,
                embedding_status="failed",
                embedding_retry_count=MAX_EMBEDDING_RETRIES,
                created_at=OLD,
            ),
            # Not a backlog: settled, deleted, or deliberately suppressed.
            _memory(owner, ctx, embedding_status="success", created_at=OLD),
            _memory(owner, ctx, embedding_status="pending", created_at=OLD, deleted_at=OLD),
            _memory(
                owner, ctx, embedding_status="failed", created_at=OLD, embedding_suppressed_at=OLD
            ),
        ]
    )
    db.add(
        DeferredPointDelete(
            memory_id=uuid4(), point_id=uuid4(), collection="kagura_memories", queued_at=aware_old
        )
    )
    requests = {}
    for status in ("pending", "cooling_off", "in_progress", "failed"):
        request = ErasureRequest(
            id=uuid4(),
            user_id=f"erase-{uuid4().hex[:8]}",
            user_email_hash="0" * 64,
            initiated_by="pytest",
            reason_code="other",
            status=status,
            requested_at=aware_old,
            scheduled_for=aware_old if status == "cooling_off" else None,
        )
        requests[status] = request
        db.add(request)
    await db.flush()
    db.add(
        VectorErasureJob(
            request_id=requests["in_progress"].id,
            memory_id=uuid4(),
            point_id=uuid4(),
            subject_pseudonym="p" * 64,
            created_at=aware_old,
        )
    )
    db.add(
        WorkspaceOwnershipForceTransferRequest(
            workspace_id=ws.id,
            target_user_id=owner,
            reason="seed",
            ownership_epoch_at_initiation=0,
            initiated_by_user_id=owner,
            initiated_by_email=f"{owner}@example.com",
            created_at=OLD,
        )
    )
    db.add(StorageObjectDeletion(storage_key=f"{ws.id}/seed-{uuid4().hex}", available_after=OLD))
    db.add_all(
        [
            FileObject(
                workspace_id=ws.id,
                sha256="a" * 64,
                size_bytes=1,
                content_type="text/plain",
                filename="seed.txt",
                created_by=owner,
                status="reserved",
                expires_at=OLD,
            ),
            FileObject(
                workspace_id=ws.id,
                sha256="b" * 64,
                size_bytes=1,
                content_type="text/plain",
                filename="seed.txt",
                created_by=owner,
                status="uploaded",
                storage_key=f"{ws.id}/seed-gc-{uuid4().hex}",
                uploaded_at=OLD,
                deleted_at=OLD,
            ),
        ]
    )
    db.add(
        EmailActionToken(
            user_id=owner,
            purpose="reset_password",
            token_hash=uuid4().hex + uuid4().hex,
            email=f"{owner}@example.com",
            expires_at=OLD,
        )
    )
    db.add(
        EmailSignupRequest(
            email=f"signup-{uuid4().hex[:8]}@example.com",
            token_hash=uuid4().hex + uuid4().hex,
            expires_at=OLD,
        )
    )
    db.add(UserKnownDevice(user_id=owner, device_hash="d" * 64, last_seen=OLD))
    # Upload malware scan (#565): one live row in every scan state, plus rows
    # no scan backlog counts (not yet due, deleted, clean).
    future = datetime(2999, 1, 1)

    def _scanned(sha: str, scan_status: str, **values: Any) -> FileObject:
        return FileObject(
            workspace_id=ws.id,
            sha256=sha * 64,
            size_bytes=1,
            content_type="text/plain",
            filename="seed.txt",
            created_by=owner,
            status="uploaded",
            storage_key=f"{ws.id}/seed-scan-{uuid4().hex}",
            uploaded_at=OLD,
            scan_status=scan_status,
            **values,
        )

    db.add_all(
        [
            _scanned("c", "unscanned"),
            _scanned("d", "pending", scan_next_attempt_at=OLD),
            _scanned("e", "pending", scan_next_attempt_at=future),
            _scanned("f", "clean", scanned_at=OLD, scan_etag="etag-seed"),
            _scanned("1", "infected", scanned_at=OLD, scan_signature=SEED_SIGNATURE),
            _scanned("2", "error", scanned_at=OLD, scan_error="scan_failed"),
            _scanned("3", "infected", scanned_at=OLD, deleted_at=OLD),
        ]
    )
    await db.flush()
    expected = {
        "embedding_pending": 2,
        "embedding_processing": 1,
        "embedding_failed_retryable": 1,
        "embedding_failed_stalled": 1,
        "deferred_point_deletes": 1,
        "erasure_pending": 1,
        "erasure_cooling_off": 1,
        "erasure_cooling_off_due": 1,
        "erasure_in_progress": 1,
        "erasure_failed": 1,
        "vector_erasure_jobs": 1,
        "force_transfer_requests_pending": 1,
        "storage_object_deletions_due": 1,
        "file_reservations_expired": 1,
        "file_soft_deleted_due": 2,  # the GC row and the deleted infected one
        "email_action_tokens_spent": 1,
        "email_signup_requests_expired": 1,
        "known_devices_stale": 1,
        "files_awaiting_scan": 1,
        "files_scan_failed": 1,
        "files_quarantined": 1,
        "files_unscanned": 1,
    }
    return owner, ws, ctx, expected


class TestBacklogs:
    async def test_backlogs_match_the_tables(self, db_session) -> None:
        settings = Settings(_env_file=None, file_scan_enabled=True)
        before = {r.name: r for r in await read_backlogs(db_session, NOW, settings)}
        _, _, _, expected = await _seed_backlogs(db_session)
        after = {r.name: r for r in await read_backlogs(db_session, NOW, settings)}
        for name, reading in after.items():
            assert reading.error_class is None, (name, reading.error_class)
        for name, delta in expected.items():
            assert after[name].count == before[name].count + delta, name
            # Every seeded queue's oldest item is older than anything else.
            assert after[name].oldest_at is not None, name
            assert after[name].oldest_at <= OLD.replace(tzinfo=UTC) + timedelta(seconds=1), name

    async def test_scan_off_shows_only_the_quarantine(self, db_session) -> None:
        """With FILE_SCAN_ENABLED off the infected files are still counted (#565)."""
        await _seed_backlogs(db_session)
        names = {r.name for r in await read_backlogs(db_session, NOW, Settings(_env_file=None))}
        assert {n for n in names if n.startswith("files_")} == {"files_quarantined"}


# ---------------------------------------------------------------------------
# The probe never resolves a workspace's own key
# ---------------------------------------------------------------------------


async def test_probe_uses_the_platform_key_with_a_workspace_key_stored(
    db_session, monkeypatch, fernet_api_key_secret
) -> None:
    from services import platform_credentials
    from services.llm_providers import ProviderResponse, Usage
    from utils.encryption import get_encryptor

    owner, ws, _ = await _scope(db_session, "byok")
    db_session.add(
        ExternalAPIKey(
            key_name="openai_api_key",
            provider="openai",
            encrypted_value=get_encryptor().encrypt("sk-workspace"),
            user_id=owner,
            workspace_id=ws.id,
        )
    )
    await db_session.commit()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-platform")
    seen: list[str] = []

    class _Stub:
        def __init__(self, api_key: str) -> None:
            seen.append(api_key)

        async def complete_json(self, prompt: str, system_prompt: str | None = None, **_: Any):
            return ProviderResponse(content="{}", usage=Usage(total=1, input=1, output=0, cached=0))

    monkeypatch.setitem(platform_credentials._API_KEY_PROVIDERS, "openai", _Stub)
    try:
        settings = Settings(_env_file=None)
        result = await run_probe(
            ProbeTarget("completion", "openai", "m1", ("analysis",)),
            ProbeClients(settings),
            settings,
        )
        assert result.ok
        assert seen == ["sk-platform"]
    finally:
        await db_session.execute(
            text("DELETE FROM external_api_keys WHERE workspace_id = :ws"), {"ws": ws.id}
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# The real route
# ---------------------------------------------------------------------------


async def test_route_end_to_end_aggregates_only(db_session, monkeypatch) -> None:
    from api.main import app
    from api.routes import admin_operations
    from auth.dependencies import get_current_user_optional
    from config.settings import get_settings
    from db.base import get_db

    owner, ws, ctx, _ = await _seed_backlogs(db_session)
    await db_session.execute(text("DELETE FROM scheduled_job_status"))
    await db_session.execute(text("DELETE FROM operations_llm_lanes"))
    generation = NOW - timedelta(hours=3)
    db_session.add_all(
        [
            # Ran 2 h ago with a 1 h cron interval: overdue past the 15 min grace.
            ScheduledJobStatus(
                job_id="stalled_job",
                job_name="Stalled job",
                trigger_kind="cron",
                expected_interval_seconds=3600,
                last_registered_at=generation,
                registered_since=generation,
                last_started_at=NOW - timedelta(hours=2),
                last_finished_at=NOW - timedelta(hours=2),
                last_outcome="ok",
                last_outcome_at=NOW - timedelta(hours=2),
                last_success_at=NOW - timedelta(hours=2),
                last_counts={"deleted": 4},
            ),
            ScheduledJobStatus(
                job_id="failing_job",
                job_name="Failing job",
                trigger_kind="interval",
                expected_interval_seconds=60,
                last_registered_at=generation,
                registered_since=generation,
                last_started_at=NOW - timedelta(seconds=30),
                last_finished_at=NOW - timedelta(seconds=29),
                last_outcome="failed",
                last_outcome_at=NOW - timedelta(seconds=29),
                consecutive_failures=3,
                last_error_class="ConnectionRefusedError",
            ),
            ScheduledJobStatus(
                job_id="operations_probe",
                job_name="Operations status: LLM lane probe (Issue #2768)",
                trigger_kind="interval",
                expected_interval_seconds=900,
                last_registered_at=generation,
                registered_since=generation,
            ),
            # A stored row carrying data where only labels and class names
            # belong (a writer that bypassed the recorder's sanitisers): the
            # route sanitises again on the way out.
            ScheduledJobStatus(
                job_id="leaky_job",
                job_name="Leaky job",
                trigger_kind="interval",
                expected_interval_seconds=60,
                last_registered_at=generation,
                registered_since=generation,
                last_started_at=NOW - timedelta(seconds=20),
                last_finished_at=NOW - timedelta(seconds=19),
                last_outcome="failed",
                last_outcome_at=NOW - timedelta(seconds=19),
                consecutive_failures=1,
                last_error_class=f"ValueError: {ws.id} {owner}@example.com",
                last_counts={
                    "deleted": 2,
                    "workspace": str(ws.id),
                    f"ctx_{ctx.id.hex}": 1,
                    "note": f"{owner}@example.com",
                },
            ),
            # Left over from an older registration: not registered, never overdue.
            ScheduledJobStatus(
                job_id="retired_job",
                job_name="Retired job",
                trigger_kind="cron",
                expected_interval_seconds=60,
                last_registered_at=generation - timedelta(days=1),
                registered_since=generation - timedelta(days=1),
            ),
            OperationsLlmLane(
                lane="sleep_judge",
                provider="openai",
                model="judge-route-model",
                credential_present=True,
                snapshot_at=NOW - timedelta(minutes=1),
                last_probe_at=NOW - timedelta(minutes=2),
                last_probe_outcome="failed",
                last_probe_latency_ms=20000,
                last_probe_error_class="TimeoutError",
                probe_consecutive_failures=2,
            ),
            OperationsLlmLane(
                lane="rerank",
                provider="self_hosted",
                model="rr",
                credential_present=True,
                snapshot_at=NOW - timedelta(minutes=1),
            ),
            OperationsLlmLane(
                lane="analysis",
                provider="openai",
                model="analysis-route-model",
                credential_present=True,
                snapshot_at=NOW - timedelta(minutes=1),
                last_probe_at=NOW - timedelta(minutes=2),
                last_probe_outcome="failed",
                last_probe_latency_ms=5,
                last_probe_error_class=f"APIError: {ctx.id} {owner}@example.com",
                probe_consecutive_failures=1,
            ),
        ]
    )
    db_session.add(_call(model="judge-route-model", user_id=owner, workspace_id=ws.id))
    await db_session.flush()

    async def _db():
        yield db_session

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    monkeypatch.setattr(get_settings(), "file_scan_enabled", True)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    admin_operations._single_flight.clear()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/admin/operations/status")
    finally:
        app.dependency_overrides.clear()
        admin_operations._single_flight.clear()

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    jobs = {j["job_id"]: j for j in body["jobs"]}
    assert jobs["stalled_job"]["overdue"] is True
    assert jobs["stalled_job"]["counts"] == {"deleted": 4}
    failing = jobs["failing_job"]
    assert (failing["last_outcome"], failing["error_class"], failing["consecutive_failures"]) == (
        "failed",
        "ConnectionRefusedError",
        3,
    )
    assert failing["overdue"] is False
    leaky = jobs["leaky_job"]
    assert (leaky["counts"], leaky["error_class"]) == ({"deleted": 2}, "Exception")
    assert jobs["retired_job"]["registered"] is False
    assert jobs["retired_job"]["overdue"] is False
    assert body["probe_enabled"] is True

    backlogs = {b["name"]: b for b in body["backlogs"]}
    assert backlogs["erasure_pending"]["count"] >= 1
    assert backlogs["erasure_pending"]["oldest_age_seconds"] > 365 * 86400
    for name in (
        "files_awaiting_scan",
        "files_scan_failed",
        "files_quarantined",
        "files_unscanned",
    ):
        assert backlogs[name]["count"] >= 1, name
        assert backlogs[name]["oldest_age_seconds"] > 365 * 86400, name
        assert backlogs[name]["error_class"] is None, name
    assert backlogs["files_awaiting_scan"]["drained_by"] == ["file_scan_sweeper"]
    assert backlogs["files_quarantined"]["drained_by"] == ["file_quarantine_expiry"]
    assert backlogs["files_scan_failed"]["drained_by"] == []

    lanes = {lane["lane"]: lane for lane in body["llm"]}
    judge = lanes["sleep_judge"]
    assert judge["probe"]["last_outcome"] == "failed"
    assert judge["probe"]["error_class"] == "TimeoutError"
    assert judge["probe"]["consecutive_failures"] == 2
    assert lanes["rerank"]["usage_recorded"] is False
    assert lanes["rerank"]["probe"] is None
    assert lanes["analysis"]["probe"]["error_class"] == "Exception"

    # Aggregates only: no identifier, no address, no message.
    raw = json.dumps(body)
    assert not UUID_RE.search(raw), UUID_RE.search(raw)
    assert "@" not in raw
    for value in (
        owner,
        str(ws.id),
        str(ctx.id),
        ws.name,
        ctx.name,
        "seed.txt",
        SEED_SIGNATURE,
        "etag-seed",
    ):
        assert value not in raw
    for job in body["jobs"]:
        assert job["error_class"] is None or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", job["error_class"]
        )


async def test_the_read_is_read_only_and_bounded(db_session) -> None:
    """The route's transaction refuses writes; past the budget, entries say so."""
    import itertools

    from sqlalchemy.exc import DBAPIError

    await db_session.execute(text("DELETE FROM operations_llm_lanes"))
    db_session.add(
        OperationsLlmLane(
            lane="embedding",
            provider="openai",
            model="e1",
            credential_present=True,
            snapshot_at=NOW,
        )
    )
    await db_session.flush()
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(100.0))
    document = await read_operations_status(
        db_session, now=NOW, settings=Settings(_env_file=None), clock=lambda: next(ticks)
    )
    assert document.backlogs
    assert {b.error_class for b in document.backlogs} == {"DeadlineExceeded"}
    assert all(b.count is None for b in document.backlogs)
    assert [lane.error_class for lane in document.llm] == ["DeadlineExceeded"]
    assert document.llm[0].calls_last_day is None

    with pytest.raises(DBAPIError, match="read-only"):
        await db_session.execute(text("DELETE FROM operations_llm_lanes"))
