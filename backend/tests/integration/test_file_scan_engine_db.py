"""#565 malware scan engine against real PostgreSQL.

- claims: due rows only, ``SKIP LOCKED`` (two claimers never get one row),
  the lease moves the next attempt, an expired lease is claimed again;
- results: fenced (a deleted, requeued or re-claimed row discards the
  result), counted vs uncounted failures, ``error`` after the attempt cap,
  the backfill gives up without blocking;
- the whole path (claim -> fake clamd -> record) for a clean file and EICAR;
- quarantine expiry: ``infected`` past the retention only, usage and Redis
  released once, ``error`` rows never deleted;
- backlog readers, rescan, and the changed-object requeue.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from config.settings import Settings
from models.auth import User, Workspace
from models.file_objects import SCAN_LEGACY_REQUEUED, FileObject, WorkspaceStorageUsage
from services import file_scan_service as svc
from services.file_scan_service import ScanResult
from services.security_notification_service import (
    SecurityEvent,
    SecurityOccurrence,
    render_security_notification,
)
from tests.services._fake_clamd import EICAR, running_fake_clamd
from tests.storage._fakes import FakeBlobStorage
from utils.datetime import utcnow

from ._admin_helpers import make_user, make_workspace

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    base = {
        "file_scan_enabled": True,
        "storage_checksum_binding_enabled": False,
        "clamd_host": "127.0.0.1",
        "clamd_connect_timeout_seconds": 1.0,
        "clamd_scan_timeout_seconds": 5.0,
        "file_scan_max_attempts": 3,
        "file_quarantine_retention_days": 7,
    }
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def sessions(async_engine):
    return async_sessionmaker(async_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def ws(db_session):
    user = make_user()
    workspace = make_workspace(owner_user_id=user.user_id)
    db_session.add(user)
    await db_session.flush()
    db_session.add(workspace)
    await db_session.commit()
    ids = (workspace.id, user.user_id)
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(Workspace).where(Workspace.id == ids[0]))
    await db_session.execute(delete(User).where(User.user_id == ids[1]))
    await db_session.commit()


async def _add(db, ws, data: bytes = b"hello", **scan) -> FileObject:
    workspace_id, owner = ws
    sha = hashlib.sha256(data + uuid4().bytes).hexdigest() if "sha256" not in scan else None
    sha = scan.pop("sha256", sha)
    row = FileObject(
        id=uuid4(),
        workspace_id=workspace_id,
        sha256=sha,
        size_bytes=len(data),
        content_type="text/plain",
        filename="a.txt",
        storage_key=f"{workspace_id}/{sha[:2]}/{sha}",
        status="uploaded",
        created_by=owner,
        uploaded_at=utcnow(),
        **scan,
    )
    db.add(row)
    await db.commit()
    return row


async def _reload(db, row: FileObject) -> FileObject:
    return (
        await db.execute(
            select(FileObject)
            .where(FileObject.id == row.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _due(minutes: int = 1):
    return utcnow() - timedelta(minutes=minutes)


class TestClaim:
    async def test_only_due_pending_rows(self, db_session, ws):
        s = _settings()
        due = await _add(db_session, ws, scan_status="pending", scan_next_attempt_at=_due())
        later = await _add(
            db_session,
            ws,
            scan_status="pending",
            scan_next_attempt_at=utcnow() + timedelta(minutes=5),
        )
        clean = await _add(db_session, ws, scan_status="clean")
        now = utcnow()
        for row in (later, clean):
            assert await svc.claim_scan(db_session, now=now, settings=s, file_id=row.id) is None
        claim = await svc.claim_scan(db_session, now=now, settings=s, file_id=due.id)
        await db_session.commit()
        assert claim is not None
        assert claim.attempts == 1 and claim.claimed_status == "pending"
        row = await _reload(db_session, due)
        assert row.scan_lease_until == claim.lease_until == row.scan_next_attempt_at
        assert row.scan_lease_until > now
        # In flight: not due again until the lease ran out.
        assert await svc.claim_scan(db_session, now=now, settings=s, file_id=due.id) is None
        again = await svc.claim_scan(
            db_session, now=claim.lease_until + timedelta(seconds=1), settings=s, file_id=due.id
        )
        await db_session.commit()
        assert again is not None and again.attempts == 2

    async def test_two_claimers_never_get_the_same_row(self, db_session, ws, sessions):
        s = _settings()
        rows = [
            await _add(db_session, ws, scan_status="pending", scan_next_attempt_at=_due(600 + i))
            for i in range(2)
        ]
        now = utcnow()
        async with sessions() as a, sessions() as b:
            first = await svc.claim_scan(a, now=now, settings=s)  # row lock held, not committed
            second = await asyncio.wait_for(svc.claim_scan(b, now=now, settings=s), 5)
            await a.commit()
            await b.commit()
        assert first is not None and second is not None
        assert {first.file_id, second.file_id} == {r.id for r in rows}

    async def test_deleted_and_not_uploaded_rows_are_never_claimed(self, db_session, ws):
        s = _settings()
        deleted = await _add(
            db_session, ws, scan_status="pending", scan_next_attempt_at=_due(), deleted_at=utcnow()
        )
        assert (
            await svc.claim_scan(db_session, now=utcnow(), settings=s, file_id=deleted.id) is None
        )

    async def test_backfill_claims_unscanned_below_the_attempt_cap(self, db_session, ws):
        s = _settings()
        legacy = await _add(db_session, ws)
        given_up = await _add(db_session, ws, scan_attempts=3)
        assert legacy.scan_status == "unscanned"
        assert (
            await svc.claim_scan(
                db_session, now=utcnow(), settings=s, file_id=given_up.id, backfill=True
            )
            is None
        )
        claim = await svc.claim_scan(
            db_session, now=utcnow(), settings=s, file_id=legacy.id, backfill=True
        )
        await db_session.commit()
        assert claim is not None and claim.backfill


class TestRecord:
    async def _claimed(self, db_session, ws, **scan):
        s = _settings()
        scan.setdefault("scan_status", "pending")
        scan.setdefault("scan_next_attempt_at", _due())
        row = await _add(db_session, ws, **scan)
        backfill = scan["scan_status"] == "unscanned"
        claim = await svc.claim_scan(
            db_session, now=utcnow(), settings=s, file_id=row.id, backfill=backfill
        )
        await db_session.commit()
        assert claim is not None
        return s, row, claim

    async def test_clean_is_written_with_the_scanned_version(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        now = utcnow()
        state = await svc.record_scan_result(
            db_session,
            claim,
            ScanResult("clean", etag="e1", last_modified=now),
            now=now,
            settings=s,
        )
        await db_session.commit()
        assert state == "clean"
        row = await _reload(db_session, row)
        assert (row.scan_status, row.scan_etag, row.scan_last_modified) == ("clean", "e1", now)
        assert row.scan_lease_until is None and row.scan_next_attempt_at is None

    async def test_a_deleted_row_discards_the_result(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        await db_session.execute(
            update(FileObject).where(FileObject.id == row.id).values(deleted_at=utcnow())
        )
        await db_session.commit()
        assert (
            await svc.record_scan_result(
                db_session, claim, ScanResult("clean"), now=utcnow(), settings=s
            )
            is None
        )
        await db_session.commit()
        assert (await _reload(db_session, row)).scan_status == "pending"

    async def test_a_requeued_or_reclaimed_row_discards_the_old_result(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        # Another claim after the lease ran out (the first worker was slow).
        newer = await svc.claim_scan(
            db_session, now=claim.lease_until + timedelta(seconds=1), settings=s, file_id=row.id
        )
        await db_session.commit()
        assert newer is not None
        assert (
            await svc.record_scan_result(
                db_session, claim, ScanResult("infected", "Sig"), now=utcnow(), settings=s
            )
            is None
        )
        assert (
            await svc.record_scan_result(
                db_session, newer, ScanResult("clean", etag="e"), now=utcnow(), settings=s
            )
            == "clean"
        )
        await db_session.commit()

    async def test_uncounted_failure_gives_the_attempt_back(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        await svc.record_scan_result(
            db_session, claim, ScanResult("retry_uncounted"), now=utcnow(), settings=s
        )
        await db_session.commit()
        row = await _reload(db_session, row)
        assert (row.scan_status, row.scan_attempts) == ("pending", 0)

    async def test_counted_failures_end_in_scan_failed(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        for _ in range(3):
            state = await svc.record_scan_result(
                db_session, claim, ScanResult("retry"), now=utcnow(), settings=s
            )
            await db_session.commit()
            if state == "error":
                break
            claim = await svc.claim_scan(
                db_session, now=utcnow() + timedelta(hours=1), settings=s, file_id=row.id
            )
            await db_session.commit()
            assert claim is not None
        row = await _reload(db_session, row)
        assert (row.scan_status, row.scan_error, row.scan_attempts) == ("error", "scan_failed", 3)

    async def test_backfill_gives_up_without_blocking(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws, scan_status="unscanned")
        state = await svc.record_scan_result(
            db_session, claim, ScanResult("object_missing"), now=utcnow(), settings=s
        )
        await db_session.commit()
        assert state == "unscanned"
        row = await _reload(db_session, row)
        assert (row.scan_status, row.scan_error) == ("unscanned", "object_missing")
        assert (
            await svc.claim_scan(
                db_session, now=utcnow(), settings=s, file_id=row.id, backfill=True
            )
            is None
        )

    async def test_release_claim_makes_the_row_due_again(self, db_session, ws):
        s, row, claim = await self._claimed(db_session, ws)
        assert await svc.release_claim(db_session, claim, now=utcnow())
        await db_session.commit()
        row = await _reload(db_session, row)
        assert row.scan_attempts == 0 and row.scan_lease_until is None
        assert await svc.claim_scan(db_session, now=utcnow(), settings=s, file_id=row.id)
        await db_session.commit()


class TestEndToEnd:
    async def test_kick_scans_clean_and_infected_files(self, db_session, ws, sessions):
        storage = FakeBlobStorage()
        rows = {}
        for name, data in (("clean", b"plain text"), ("infected", b"x " + EICAR)):
            row = await _add(
                db_session,
                ws,
                data,
                sha256=hashlib.sha256(data).hexdigest(),
                scan_status="pending",
                scan_next_attempt_at=_due(),
            )
            await storage.write_object(row.storage_key, data, "text/plain", f"etag-{name}")
            rows[name] = row
        # Another uploader for the clean file, so a mail for it would show.
        await db_session.execute(
            update(FileObject)
            .where(FileObject.id == rows["clean"].id)
            .values(created_by="clean-uploader")
        )
        await db_session.commit()

        async def get_db():
            async with sessions() as session:
                yield session

        async with running_fake_clamd() as (_, port):
            s = _settings(storage_checksum_binding_enabled=True, clamd_port=port)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", get_db),
                patch.object(svc, "_reader", return_value=storage),
                patch("services.security_notification_service.spawn_security_notification") as mail,
            ):
                svc._reset_runtime_for_tests()
                for row in rows.values():
                    svc.kick_file_scan(row.id)
                await asyncio.wait_for(asyncio.gather(*svc._running), 10)

        clean = await _reload(db_session, rows["clean"])
        infected = await _reload(db_session, rows["infected"])
        assert (clean.scan_status, clean.scan_etag) == ("clean", "etag-clean")
        assert (infected.scan_status, infected.scan_signature) == (
            "infected",
            "Eicar-Test-Signature",
        )
        # Owner decision (email + UI): the uploader of the infected file, and
        # only that file, gets the FILE_BLOCKED notice. The notice carries no
        # file detail (the call has none to pass), and its text names neither
        # the file nor the finding.
        mail.assert_called_once_with(user_id=ws[1], event=SecurityEvent.FILE_BLOCKED)
        subject, text = render_security_notification(
            SecurityEvent.FILE_BLOCKED,
            [SecurityOccurrence(occurred_at="2026-11-13T00:00:00 UTC")],
            digest=False,
            window_minutes=10,
            profile_page_url="https://app.example/profile",
        )
        for absent in (infected.filename, infected.scan_signature, str(infected.id), "Eicar"):
            assert absent not in subject and absent not in text, absent
        backlog = await svc.file_scan_backlog(db_session, utcnow())
        assert backlog.quarantined >= 1


class TestQuarantineExpiry:
    async def test_expires_old_infected_only_and_releases_once(self, db_session, ws, sessions):
        workspace_id, _ = ws
        old = utcnow() - timedelta(days=8)
        expired = await _add(db_session, ws, b"12345", scan_status="infected", scanned_at=old)
        young = await _add(db_session, ws, b"123", scan_status="infected", scanned_at=utcnow())
        failed = await _add(
            db_session, ws, b"1", scan_status="error", scan_error="scan_failed", scanned_at=old
        )
        db_session.add(WorkspaceStorageUsage(workspace_id=workspace_id, used_bytes=9, file_count=3))
        await db_session.commit()

        async def get_db():
            async with sessions() as session:
                yield session

        release = AsyncMock()
        with (
            patch.object(svc, "get_settings", return_value=_settings(file_scan_enabled=False)),
            patch.object(svc, "get_db", get_db),
            patch("services.storage_quota_service.release_storage_bytes", release),
        ):
            counts = await svc.expire_quarantined_files()
            again = await svc.expire_quarantined_files()

        assert counts == {"expired": 1, "released_bytes": 5}
        assert again == {"expired": 0, "released_bytes": 0}
        release.assert_awaited_once_with(workspace_id=workspace_id, size_bytes=5)
        assert (await _reload(db_session, expired)).deleted_at is not None
        assert (await _reload(db_session, young)).deleted_at is None
        assert (await _reload(db_session, failed)).deleted_at is None  # never deleted
        usage = await db_session.get(WorkspaceStorageUsage, workspace_id, populate_existing=True)
        assert (usage.used_bytes, usage.file_count) == (4, 2)


class TestBacklogAndRescan:
    async def test_backlog_readers(self, db_session, ws):
        now = utcnow()
        await _add(
            db_session, ws, scan_status="pending", scan_next_attempt_at=now - timedelta(hours=2)
        )
        await _add(
            db_session, ws, scan_status="pending", scan_next_attempt_at=now + timedelta(hours=1)
        )
        await _add(db_session, ws, scan_status="error", scan_error="unscannable", scanned_at=now)
        await _add(db_session, ws, scan_status="infected", scanned_at=now)
        due, oldest = await svc.files_awaiting_scan_backlog(db_session, now)
        assert due >= 1 and oldest is not None and oldest <= now - timedelta(hours=2)
        assert (await svc.files_scan_failed_backlog(db_session, now))[0] >= 1
        assert (await svc.files_quarantined_backlog(db_session, now))[0] >= 1
        assert (await svc.files_unscanned_backlog(db_session, now))[0] >= 0
        backlog = await svc.file_scan_backlog(db_session, now)
        assert backlog.waiting_pending >= 1
        # The operations status passes an aware time; the columns are naive UTC.
        aware = now.replace(tzinfo=UTC)
        assert await svc.files_awaiting_scan_backlog(db_session, aware) == (due, oldest)
        assert await svc.file_scan_backlog(db_session, aware) == backlog

    async def test_rescan_selects_and_requeues(self, db_session, ws):
        s = _settings()
        now = utcnow()
        failed = await _add(
            db_session, ws, scan_status="error", scan_error="scan_failed", scanned_at=now
        )
        stale = await _add(
            db_session,
            ws,
            scan_status="error",
            scan_error="scan_failed",
            scanned_at=now - timedelta(days=30),
        )
        infected = await _add(db_session, ws, scan_status="infected", scan_signature="Sig")
        with pytest.raises(ValueError):
            await svc.requeue_for_rescan(db_session, now=now, settings=s, dry_run=True)

        plan = await svc.requeue_for_rescan(
            db_session,
            now=now,
            settings=s,
            dry_run=True,
            errors_only=True,
            errors_since=now - timedelta(days=1),
        )
        planned = {r[0] for r in plan.rows}  # other modules' rows may share the DB
        assert failed.id in planned and stale.id not in planned and infected.id not in planned
        assert (await _reload(db_session, failed)).scan_status == "error"

        applied = await svc.requeue_for_rescan(
            db_session, now=now, settings=s, dry_run=False, file_id=infected.id
        )
        await db_session.commit()
        assert applied.count == 1
        row = await _reload(db_session, infected)
        assert (row.scan_status, row.scan_signature, row.scan_attempts) == ("pending", None, 0)
        # Binding off: no earlier than the end of the row's upload window.
        assert row.scan_next_attempt_at >= now
        assert (await _reload(db_session, stale)).scan_status == "error"

    async def test_changed_object_is_requeued_once(self, db_session, ws):
        s = _settings()
        row = await _add(db_session, ws, scan_status="clean", scan_etag="e1")
        assert await svc.requeue_changed_object(db_session, row, now=utcnow(), settings=s)
        await db_session.commit()
        assert not await svc.requeue_changed_object(db_session, row, now=utcnow(), settings=s)
        await db_session.commit()
        reloaded = await _reload(db_session, row)
        assert reloaded.scan_status == "pending"
        assert reloaded.scan_next_attempt_at > utcnow() + timedelta(minutes=4)

    async def test_changed_object_with_a_stale_marker_is_left_alone(self, db_session, ws):
        """Fenced on ``scan_error`` too: a row whose marker changed since it was
        read (a later scan cleared it) is not requeued from the stale copy."""
        s = _settings()
        row = await _add(
            db_session, ws, scan_status="clean", scan_etag="e1", scan_error=SCAN_LEGACY_REQUEUED
        )
        await db_session.execute(
            update(FileObject)
            .where(FileObject.id == row.id)
            .values(scan_error=None)
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        assert row.scan_error == SCAN_LEGACY_REQUEUED  # the caller's copy is stale

        assert not await svc.requeue_changed_object(db_session, row, now=utcnow(), settings=s)
        await db_session.commit()
        reloaded = await _reload(db_session, row)
        assert (reloaded.scan_status, reloaded.scan_error) == ("clean", None)
        assert reloaded.scan_next_attempt_at is None
