"""Upload malware scan engine (#565): the parts that need no database.

The database state machine (claim, fence, expiry, backlogs) is pinned against
Postgres in ``tests/integration/test_file_scan_engine_db.py``; clamd is the
fake INSTREAM server of ``tests/services/_fake_clamd.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.settings import Settings
from models.file_objects import SCAN_ERRORS, SCAN_LEGACY_REQUEUED, SCAN_STATUSES, FileObject
from services import file_scan_service as svc
from services.clamd_client import ClamdUnavailable
from services.file_scan_service import (
    ClaimedScan,
    ScanResult,
    ensure_downloadable,
    object_matches_scan,
    scan_backoff,
    scan_claimed,
    scan_eligible_at,
)
from tasks.job_runs import JobFailed, job_failed
from tests.services._fake_clamd import EICAR, FakeClamd, free_port, running_fake_clamd
from tests.storage._fakes import FakeBlobStorage
from utils.exceptions import ExternalServiceError, FileBlockedError, FileScanPendingError

NOW = datetime(2026, 11, 13, 12, 0, 0)


def _settings(**overrides) -> Settings:
    base = {
        "file_scan_enabled": True,
        "storage_checksum_binding_enabled": False,
        "presign_put_ttl_seconds": 300,
        "file_scan_put_expiry_margin_seconds": 30,
        "clamd_host": "127.0.0.1",
        "clamd_connect_timeout_seconds": 1.0,
        "clamd_scan_timeout_seconds": 5.0,
        "file_scan_max_attempts": 3,
    }
    base.update(overrides)
    return Settings(**base)


def _claim(data: bytes = b"hello", *, status: str = "pending", attempts: int = 1, **kw):
    sha = kw.pop("sha256", hashlib.sha256(data).hexdigest())
    ws = uuid4()
    return ClaimedScan(
        file_id=kw.pop("file_id", uuid4()),
        workspace_id=ws,
        storage_key=kw.pop("storage_key", f"{ws}/{sha[:2]}/{sha}"),
        sha256=sha,
        size_bytes=kw.pop("size_bytes", len(data)),
        created_by=kw.pop("created_by", "u-1"),
        claimed_status=status,
        attempts=attempts,
        lease_until=NOW + timedelta(minutes=6),
    )


# ---------------------------------------------------------------------------
# Eligibility and backoff
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_binding_on_is_eligible_at_once(self):
        s = _settings(storage_checksum_binding_enabled=True)
        assert (
            scan_eligible_at(
                now=NOW,
                reservation_expires_at=NOW + timedelta(minutes=4),
                server_written=False,
                settings=s,
            )
            == NOW
        )

    def test_server_written_is_eligible_at_once(self):
        s = _settings()
        assert (
            scan_eligible_at(now=NOW, reservation_expires_at=None, server_written=True, settings=s)
            == NOW
        )

    def test_binding_off_waits_for_the_put_url_plus_margin(self):
        s = _settings()
        expires = NOW + timedelta(seconds=200)
        assert scan_eligible_at(
            now=NOW, reservation_expires_at=expires, server_written=False, settings=s
        ) == expires + timedelta(seconds=30)

    def test_binding_off_after_the_window_is_now(self):
        s = _settings()
        expires = NOW - timedelta(hours=1)
        assert (
            scan_eligible_at(
                now=NOW, reservation_expires_at=expires, server_written=False, settings=s
            )
            == NOW
        )

    def test_binding_off_unknown_window_counts_a_full_ttl(self):
        """Restore / changed object: a PUT URL minted up to now may still be live."""
        s = _settings()
        assert scan_eligible_at(
            now=NOW, reservation_expires_at=None, server_written=False, settings=s
        ) == NOW + timedelta(seconds=330)

    def test_backoff_is_capped(self):
        assert scan_backoff(1) == timedelta(seconds=30)
        assert scan_backoff(2) == timedelta(seconds=60)
        assert scan_backoff(4) == timedelta(seconds=240)
        assert scan_backoff(5) == timedelta(minutes=5)
        assert scan_backoff(50) == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# The download gate: an allow-list
# ---------------------------------------------------------------------------

_TRUTH_TABLE = {
    # scan_status: (enabled outcome, disabled outcome)
    "unscanned": (None, None),
    "pending": (FileScanPendingError, None),
    "clean": (None, None),
    "infected": (FileBlockedError, FileBlockedError),
    "error": (FileBlockedError, None),
}


def _file(scan_status: str | None) -> FileObject:
    return FileObject(id=uuid4(), scan_status=scan_status)


class TestEnsureDownloadable:
    def test_truth_table_covers_every_check_value(self):
        check = next(
            c
            for c in FileObject.__table__.constraints
            if getattr(c, "name", None) == "valid_file_scan_status"
        )
        values = set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))
        assert values == set(_TRUTH_TABLE) == set(SCAN_STATUSES)

    @pytest.mark.parametrize("status", sorted(_TRUTH_TABLE))
    @pytest.mark.parametrize("enabled", [True, False])
    def test_truth_table(self, status, enabled):
        expected = _TRUTH_TABLE[status][0 if enabled else 1]
        file = _file(status)
        if expected is None:
            ensure_downloadable(file, _settings(file_scan_enabled=enabled))
            return
        with pytest.raises(expected) as exc_info:
            ensure_downloadable(file, _settings(file_scan_enabled=enabled))
        assert exc_info.value.details["file_id"] == str(file.id)
        assert exc_info.value.details["scan_status"] == status
        assert exc_info.value.status_code == 409

    @pytest.mark.parametrize("status", ["quarantined", "", None])
    def test_unknown_values_are_blocked_when_enabled(self, status):
        with pytest.raises(FileBlockedError):
            ensure_downloadable(_file(status), _settings())
        ensure_downloadable(_file(status), _settings(file_scan_enabled=False))

    def test_refusals_carry_codes_and_retry_after(self):
        with pytest.raises(FileScanPendingError) as pending:
            ensure_downloadable(_file("pending"), _settings())
        assert pending.value.error_code == "SCAN-001"
        assert pending.value.response_headers == {"Retry-After": "30"}
        assert pending.value.log_level == "warning"
        with pytest.raises(FileBlockedError) as blocked:
            ensure_downloadable(_file("infected"), _settings())
        assert blocked.value.error_code == "SCAN-002"
        assert blocked.value.log_level == "warning"
        assert "signature" not in str(blocked.value.details)


class TestObjectMatchesScan:
    def _clean(self, **kw):
        base = {
            "scan_etag": "e1",
            "size_bytes": 5,
            "scan_last_modified": NOW,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def test_same_object(self):
        assert object_matches_scan(
            self._clean(), {"etag": "e1", "size_bytes": 5, "last_modified": NOW}
        )

    @pytest.mark.parametrize(
        "meta",
        [
            {"etag": "e2", "size_bytes": 5, "last_modified": NOW},
            {"etag": "e1", "size_bytes": 6, "last_modified": NOW},
            # Same ETag (e.g. an MD5 collision), rewritten later.
            {"etag": "e1", "size_bytes": 5, "last_modified": NOW + timedelta(seconds=1)},
            {"etag": "e1", "size_bytes": 5},
        ],
    )
    def test_any_difference_is_a_change(self, meta):
        assert not object_matches_scan(self._clean(), meta)

    def test_no_recorded_etag_never_matches(self):
        assert not object_matches_scan(
            self._clean(scan_etag=None), {"etag": "", "size_bytes": 5, "last_modified": NOW}
        )


# ---------------------------------------------------------------------------
# One scan: storage -> clamd, no database
# ---------------------------------------------------------------------------


async def _scan(storage, claim, port, **settings_kw) -> ScanResult:
    return await scan_claimed(
        claim, storage=storage, settings=_settings(clamd_port=port, **settings_kw)
    )


async def _stored(data: bytes, claim: ClaimedScan, *, etag: str = "etag-1") -> FakeBlobStorage:
    storage = FakeBlobStorage()
    await storage.write_object(claim.storage_key, data, "text/plain", etag)
    storage.last_modified[claim.storage_key] = NOW
    return storage


class TestScanClaimed:
    async def test_clean_records_the_scanned_version(self):
        claim = _claim(b"hello")
        storage = await _stored(b"hello", claim)
        async with running_fake_clamd() as (fake, port):
            result = await _scan(storage, claim, port)
        assert result == ScanResult("clean", etag="etag-1", last_modified=NOW)
        assert fake.streams == [b"hello"]

    async def test_eicar_is_infected(self):
        data = b"head " + EICAR
        claim = _claim(data)
        storage = await _stored(data, claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "infected"
        assert result.signature == "Eicar-Test-Signature"
        assert result.etag == "etag-1"

    async def test_missing_object(self):
        claim = _claim()
        async with running_fake_clamd() as (fake, port):
            result = await _scan(FakeBlobStorage(), claim, port)
        assert result.kind == "object_missing"
        assert fake.connections == 0

    async def test_replaced_bytes_of_a_new_upload_are_a_digest_mismatch(self):
        claim = _claim(b"hello")
        storage = await _stored(b"HELLO", claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "digest_mismatch"

    async def test_a_finding_wins_over_a_digest_mismatch(self):
        claim = _claim(b"hello")
        storage = await _stored(EICAR, claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "infected"

    async def test_backfill_mismatch_keeps_the_verdict(self):
        """Legacy rows' digests were never verified: the verdict stands."""
        claim = _claim(b"hello", status="unscanned")
        storage = await _stored(b"HELLO", claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "clean"

    @pytest.mark.parametrize("legacy", ["backfill", "requeued"])
    async def test_a_legacy_mismatch_stays_on_the_clean_verdict(self, legacy):
        """A later restore must still see that this file's digest never matched."""
        if legacy == "backfill":
            claim = _claim(b"hello", status="unscanned")
        else:
            claim = replace(_claim(b"hello"), legacy_requeued=True)
        storage = await _stored(b"HELLO", claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        state, values = svc.scan_outcome(claim, result, now=NOW, settings=_settings())
        assert state == "clean"
        assert values["scan_error"] == SCAN_LEGACY_REQUEUED

    @pytest.mark.parametrize("legacy", ["backfill", "requeued"])
    async def test_a_legacy_file_whose_digest_matches_is_verified_from_then_on(self, legacy):
        if legacy == "backfill":
            claim = _claim(b"hello", status="unscanned")
        else:
            claim = replace(_claim(b"hello"), legacy_requeued=True)
        storage = await _stored(b"hello", claim)
        async with running_fake_clamd() as (_, port):
            result = await _scan(storage, claim, port)
        state, values = svc.scan_outcome(claim, result, now=NOW, settings=_settings())
        assert state == "clean"
        assert values["scan_error"] is None

    async def test_limit_finding_is_unscannable(self):
        claim = _claim()
        storage = await _stored(b"hello", claim)
        reply = b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND\0"
        async with running_fake_clamd(FakeClamd(reply=reply)) as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "unscannable"

    async def test_object_over_the_stream_limit_is_not_streamed(self):
        claim = _claim(b"0123456789")
        storage = await _stored(b"0123456789", claim)
        async with running_fake_clamd() as (fake, port):
            result = await _scan(storage, claim, port, clamd_stream_max_bytes=4)
        assert result.kind == "size_limit"
        assert fake.connections == 0

    async def test_clamd_refusing_the_size(self):
        claim = _claim(b"x" * 70000)
        storage = await _stored(b"x" * 70000, claim)
        async with running_fake_clamd(FakeClamd(stream_max_bytes=10)) as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind in ("size_limit", "retry")

    async def test_clamd_down_does_not_count(self):
        claim = _claim()
        storage = await _stored(b"hello", claim)
        result = await _scan(storage, claim, await free_port())
        assert result.kind == "retry_uncounted"

    async def test_storage_unreachable_before_streaming_does_not_count(self):
        claim = _claim()
        storage = FakeBlobStorage()

        @asynccontextmanager
        async def failing_open(key):
            raise ExternalServiceError("storage", "get_object failed: 503")
            yield  # pragma: no cover

        storage.open_object = failing_open  # type: ignore[method-assign]
        async with running_fake_clamd() as (fake, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "retry_uncounted"
        assert fake.connections == 0

    async def test_storage_read_error_mid_stream_counts(self):
        claim = _claim(b"0123456789")
        storage = await _stored(b"0123456789", claim)
        real_open = storage.open_object

        @asynccontextmanager
        async def breaking_open(key):
            async with real_open(key) as opened:

                async def chunks(size):
                    yield b"01234"
                    raise OSError("read reset")

                yield SimpleNamespace(
                    etag=opened.etag,
                    size_bytes=opened.size_bytes,
                    last_modified=opened.last_modified,
                    iter_chunks=chunks,
                )

        storage.open_object = breaking_open  # type: ignore[method-assign]
        async with running_fake_clamd() as (fake, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "retry"
        await asyncio.sleep(0.05)
        assert fake.terminated == [False]

    async def test_reset_mid_stream_counts(self):
        claim = _claim(b"y" * 300000)
        storage = await _stored(b"y" * 300000, claim)
        async with running_fake_clamd(FakeClamd(reset_after_bytes=1)) as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "retry"

    async def test_malformed_reply_counts(self):
        claim = _claim()
        storage = await _stored(b"hello", claim)
        async with running_fake_clamd(FakeClamd(reply=b"stream: OKAY\0")) as (_, port):
            result = await _scan(storage, claim, port)
        assert result.kind == "retry"


# ---------------------------------------------------------------------------
# What a result writes (pure; the fenced UPDATE is pinned in the DB tests)
# ---------------------------------------------------------------------------


class TestOutcome:
    def test_clean(self):
        state, values = svc.scan_outcome(
            _claim(),
            ScanResult("clean", etag="e", last_modified=NOW),
            now=NOW,
            settings=_settings(),
        )
        assert state == "clean"
        assert values["scan_status"] == "clean"
        assert values["scan_etag"] == "e" and values["scan_last_modified"] == NOW
        assert values["scanned_at"] == NOW
        assert values["scan_lease_until"] is None and values["scan_next_attempt_at"] is None
        assert values["scan_error"] is None and values["scan_signature"] is None

    def test_infected_keeps_the_signature(self):
        state, values = svc.scan_outcome(
            _claim(), ScanResult("infected", signature="Sig"), now=NOW, settings=_settings()
        )
        assert state == "infected"
        assert values["scan_signature"] == "Sig"

    @pytest.mark.parametrize(
        "kind", ["object_missing", "digest_mismatch", "size_limit", "unscannable"]
    )
    def test_permanent_causes_block_a_new_upload(self, kind):
        state, values = svc.scan_outcome(_claim(), ScanResult(kind), now=NOW, settings=_settings())
        assert state == "error"
        assert values["scan_status"] == "error"
        assert values["scan_error"] == kind
        assert kind in SCAN_ERRORS

    @pytest.mark.parametrize("kind", ["object_missing", "size_limit", "unscannable", "retry"])
    def test_backfill_gives_up_without_blocking(self, kind):
        claim = _claim(status="unscanned", attempts=3)
        state, values = svc.scan_outcome(claim, ScanResult(kind), now=NOW, settings=_settings())
        assert state == "unscanned"
        assert values["scan_status"] == "unscanned"
        assert values["scan_attempts"] >= 3
        assert values["scan_error"] in SCAN_ERRORS

    def test_counted_retry_backs_off(self):
        state, values = svc.scan_outcome(
            _claim(attempts=2), ScanResult("retry"), now=NOW, settings=_settings()
        )
        assert state == "pending"
        assert values == {
            "scan_lease_until": None,
            "scan_next_attempt_at": NOW + timedelta(seconds=60),
        }

    def test_last_counted_retry_is_scan_failed(self):
        state, values = svc.scan_outcome(
            _claim(attempts=3), ScanResult("retry"), now=NOW, settings=_settings()
        )
        assert state == "error"
        assert values["scan_error"] == "scan_failed"

    def test_uncounted_retry_gives_the_attempt_back(self):
        state, values = svc.scan_outcome(
            _claim(attempts=3), ScanResult("retry_uncounted"), now=NOW, settings=_settings()
        )
        assert state == "pending"
        assert values["scan_attempts"] == 2
        assert values["scan_next_attempt_at"] == NOW + timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Kick, sweep, sessions, shutdown
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _fresh_runtime():
    await svc.stop_file_scans(timeout=1.0)
    svc._reset_runtime_for_tests()
    yield
    await svc.stop_file_scans(timeout=1.0)
    svc._reset_runtime_for_tests()


class _Sessions:
    """A ``get_db`` stand-in that counts open sessions."""

    def __init__(self) -> None:
        self.open = 0
        self.opened = 0

    async def __call__(self):
        self.open += 1
        self.opened += 1
        try:
            yield MagicMock(commit=AsyncMock())
        finally:
            self.open -= 1


class TestKick:
    async def test_disabled_is_a_no_op(self):
        # A running loop and binding on, so only the switch keeps the kick off.
        s = _settings(file_scan_enabled=False, storage_checksum_binding_enabled=True)
        with patch.object(svc, "get_settings", return_value=s):
            svc.kick_file_scan(uuid4())
        assert svc._running == set()

    async def test_binding_off_is_a_no_op(self):
        with patch.object(svc, "get_settings", return_value=_settings()):
            svc.kick_file_scan(uuid4())
        assert svc._running == set()

    def test_without_a_running_loop_never_raises(self):
        with patch.object(
            svc, "get_settings", return_value=_settings(storage_checksum_binding_enabled=True)
        ):
            svc.kick_file_scan(uuid4())
        assert svc._running == set()

    async def test_queue_is_bounded(self):
        s = _settings(storage_checksum_binding_enabled=True, file_scan_concurrency=1)
        gate = asyncio.Event()

        async def slow_kick(file_id):
            await gate.wait()

        with (
            patch.object(svc, "get_settings", return_value=s),
            patch.object(svc, "_kick", slow_kick),
        ):
            for _ in range(svc.KICK_QUEUE_LIMIT + 10):
                svc.kick_file_scan(uuid4())
            assert len(svc._running) == svc.KICK_QUEUE_LIMIT
            gate.set()
            await asyncio.sleep(0)

    async def test_kick_scans_without_holding_a_session_while_streaming(self):
        claim = _claim(b"hello")
        storage = await _stored(b"hello", claim)
        sessions = _Sessions()
        seen_open: list[int] = []
        real_open = storage.open_object

        @asynccontextmanager
        async def watching_open(key):
            seen_open.append(sessions.open)
            async with real_open(key) as opened:
                yield opened

        storage.open_object = watching_open  # type: ignore[method-assign]
        record = AsyncMock(return_value="clean")
        async with running_fake_clamd() as (_, port):
            s = _settings(storage_checksum_binding_enabled=True, clamd_port=port)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", sessions),
                patch.object(svc, "_reader", return_value=storage),
                patch.object(svc, "claim_scan", AsyncMock(return_value=claim)),
                patch.object(svc, "record_scan_result", record),
            ):
                svc.kick_file_scan(claim.file_id)
                await asyncio.wait_for(asyncio.gather(*svc._running), 5)
        assert seen_open == [0]
        assert sessions.opened == 2  # claim, record — each its own short session
        assert record.await_args.args[2].kind == "clean"

    async def test_kick_swallows_errors(self):
        s = _settings(storage_checksum_binding_enabled=True)
        with (
            patch.object(svc, "get_settings", return_value=s),
            patch.object(svc, "_reader", side_effect=RuntimeError("boom")),
        ):
            svc.kick_file_scan(uuid4())
            await asyncio.wait_for(asyncio.gather(*svc._running), 5)


class TestSweep:
    async def test_disabled_touches_nothing(self):
        sessions = _Sessions()
        ping = AsyncMock()
        with (
            patch.object(svc, "get_settings", return_value=_settings(file_scan_enabled=False)),
            patch.object(svc, "get_db", sessions),
            patch.object(svc, "clamd_ping", ping),
            # Storage is readable, so only the switch keeps the sweep off.
            patch.object(svc, "_reader", return_value=FakeBlobStorage()),
        ):
            counts = await svc.run_scan_sweep()
        assert counts == {"dispatched": 0, "backfill_dispatched": 0}
        assert sessions.opened == 0
        ping.assert_not_awaited()

    async def test_unconfigured_storage_returns_quietly(self):
        sessions = _Sessions()
        with (
            patch.object(svc, "get_settings", return_value=_settings()),
            patch.object(svc, "get_db", sessions),
            patch.object(
                svc, "get_blob_storage", side_effect=ExternalServiceError("storage", "unset")
            ),
        ):
            counts = await svc.run_scan_sweep()
        assert counts["dispatched"] == 0
        assert sessions.opened == 0

    async def test_clamd_down_leaves_rows_alone(self):
        sessions = _Sessions()
        claim = AsyncMock()
        with (
            patch.object(svc, "get_settings", return_value=_settings(clamd_port=await free_port())),
            patch.object(svc, "get_db", sessions),
            patch.object(svc, "_reader", return_value=FakeBlobStorage()),
            patch.object(svc, "claim_scan", claim),
        ):
            result = await svc.run_scan_sweep()
        # The run is recorded as failed with the class name (operations status, #2768).
        assert result == job_failed(ClamdUnavailable())
        assert isinstance(result, JobFailed)
        assert result.error_class == "ClamdUnavailable"
        claim.assert_not_awaited()
        assert sessions.opened == 0

    async def test_sweep_dispatches_free_slots_and_returns_before_the_scan_ends(self):
        """The 30 s job never runs a scan inline."""
        claims = [_claim(), _claim(), _claim()]
        gate = asyncio.Event()

        async def fake_claim(db, *, now, settings, file_id=None, backfill=False):
            return None if backfill or not claims else claims.pop(0)

        async def slow_scan(claim, storage):
            await gate.wait()
            return ScanResult("clean")

        async with running_fake_clamd() as (_, port):
            s = _settings(clamd_port=port, file_scan_concurrency=2)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", _Sessions()),
                patch.object(svc, "_reader", return_value=FakeBlobStorage()),
                patch.object(svc, "claim_scan", fake_claim),
                patch.object(svc, "_scan_and_record", slow_scan),
            ):
                counts = await asyncio.wait_for(svc.run_scan_sweep(), 2)
                assert counts["dispatched"] == 2
                assert len(svc._running) == 2
                # No free slot: the next tick dispatches nothing.
                assert (await svc.run_scan_sweep())["dispatched"] == 0
                gate.set()
                await asyncio.wait_for(asyncio.gather(*svc._running), 5)
        # The workers kept draining the queue with their slots.
        assert claims == []

    async def test_backfill_only_after_the_queue_and_within_the_batch(self):
        backfill_claims = [_claim(status="unscanned") for _ in range(5)]
        asked: list[bool] = []

        async def fake_claim(db, *, now, settings, file_id=None, backfill=False):
            asked.append(backfill)
            if not backfill:
                return None
            return backfill_claims.pop(0) if backfill_claims else None

        async with running_fake_clamd() as (_, port):
            s = _settings(clamd_port=port, file_scan_concurrency=2, file_scan_backfill_batch=3)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", _Sessions()),
                patch.object(svc, "_reader", return_value=FakeBlobStorage()),
                patch.object(svc, "claim_scan", fake_claim),
                patch.object(svc, "_scan_and_record", AsyncMock(return_value=ScanResult("clean"))),
            ):
                counts = await svc.run_scan_sweep()
                await asyncio.wait_for(asyncio.gather(*svc._running), 5)
        assert counts["backfill_dispatched"] == 2
        assert len(backfill_claims) == 2  # 3 claimed in total this tick
        assert asked[0] is False  # the pending queue is asked first

    async def test_backfill_zero_disables_it(self):
        asked: list[bool] = []

        async def fake_claim(db, *, now, settings, file_id=None, backfill=False):
            asked.append(backfill)
            return None

        async with running_fake_clamd() as (_, port):
            s = _settings(clamd_port=port, file_scan_backfill_batch=0)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", _Sessions()),
                patch.object(svc, "_reader", return_value=FakeBlobStorage()),
                patch.object(svc, "claim_scan", fake_claim),
            ):
                await svc.run_scan_sweep()
        assert True not in asked


class TestStop:
    async def test_stop_cancels_and_gives_the_claim_back(self):
        claim = _claim()
        storage = await _stored(b"hello", claim)
        released = AsyncMock(return_value=True)
        async with running_fake_clamd(FakeClamd(delay=30)) as (_, port):
            s = _settings(storage_checksum_binding_enabled=True, clamd_port=port)
            with (
                patch.object(svc, "get_settings", return_value=s),
                patch.object(svc, "get_db", _Sessions()),
                patch.object(svc, "_reader", return_value=storage),
                patch.object(svc, "claim_scan", AsyncMock(return_value=claim)),
                patch.object(svc, "release_claim", released),
            ):
                svc.kick_file_scan(claim.file_id)
                await asyncio.sleep(0.3)
                assert len(svc._running) == 1
                await svc.stop_file_scans(timeout=2.0)
        assert svc._running == set()
        released.assert_awaited_once()
        assert released.await_args.args[1] == claim


# ---------------------------------------------------------------------------
# Quarantine expiry
# ---------------------------------------------------------------------------


class TestExpiry:
    async def test_batches_commit_then_release(self):
        from services.file_storage_service import FilesPurged

        ws = uuid4()
        batches = [
            FilesPurged(rows=svc.EXPIRY_BATCH, visible=svc.EXPIRY_BATCH, released={ws: 100}),
            FilesPurged(rows=1, visible=1, released={ws: 7}),
        ]
        order: list[str] = []
        purge = AsyncMock(side_effect=lambda **kw: order.append("purge") or batches.pop(0))

        class _Svc:
            def __init__(self, db):
                self.purge_quarantined_files = purge

        sessions = _Sessions()

        async def release(*, workspace_id, size_bytes):
            order.append(f"release:{size_bytes}")

        with (
            patch.object(svc, "get_settings", return_value=_settings(file_scan_enabled=False)),
            patch.object(svc, "get_db", sessions),
            patch("services.file_storage_service.FileStorageService", _Svc),
            patch("services.storage_quota_service.release_storage_bytes", release),
        ):
            counts = await svc.expire_quarantined_files()
        assert counts == {"expired": svc.EXPIRY_BATCH + 1, "released_bytes": 107}
        assert order == ["purge", "release:100", "purge", "release:7"]
        cutoff = purge.await_args.kwargs["cutoff"]
        assert isinstance(cutoff, datetime)
        assert purge.await_args.kwargs["limit"] == svc.EXPIRY_BATCH == 50


# ---------------------------------------------------------------------------
# Startup warning
# ---------------------------------------------------------------------------


class TestConfigurationLog:
    def test_warns_when_scanning_without_binding(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            svc.log_scan_configuration(_settings())
        assert any(e["event"] == "file_scan_without_checksum_binding" for e in logs)

    @pytest.mark.parametrize(
        "kw",
        [{"file_scan_enabled": False}, {"storage_checksum_binding_enabled": True}],
    )
    def test_silent_otherwise(self, kw):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            svc.log_scan_configuration(_settings(**kw))
        assert not any(e["event"] == "file_scan_without_checksum_binding" for e in logs)


class TestLifespanStopsScans:
    def test_shutdown_stops_scans_after_the_scheduler_and_before_storage_and_db(self):
        """Awaited calls after ``yield`` in ``lifespan()``, read from the AST."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text(
            encoding="utf-8"
        )
        lifespan = next(
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        )
        yield_line = next(n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield))
        awaited = [
            ast.unparse(node.value.func)
            for node in sorted(
                (n for n in ast.walk(lifespan) if isinstance(n, ast.Await)),
                key=lambda n: (n.lineno, n.col_offset),
            )
            if node.lineno > yield_line and isinstance(node.value, ast.Call)
        ]
        assert awaited.count("stop_file_scans") == 1
        assert (
            awaited.index("scheduler_leader.stop")
            < awaited.index("stop_file_scans")
            < awaited.index("close_blob_storage")
            < awaited.index("close_db")
        )


class TestNotifyUploader:
    """The uploader of an infected file gets an email."""

    def test_infected_upload_mails_its_uploader(self):
        from services.security_notification_service import SecurityEvent

        with patch("services.security_notification_service.spawn_security_notification") as spawn:
            svc._notify_uploader(_claim(created_by="user-7"))
        spawn.assert_called_once_with(user_id="user-7", event=SecurityEvent.FILE_BLOCKED)

    def test_migrated_attachment_has_no_uploader_to_mail(self):
        with patch("services.security_notification_service.spawn_security_notification") as spawn:
            svc._notify_uploader(_claim(created_by="migration:attachment:user-7"))
        spawn.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "mailed"),
        [("infected", True), ("unscannable", False), ("object_missing", False), ("clean", False)],
    )
    async def test_only_an_infected_result_mails(self, kind, mailed):
        state = {"infected": "infected", "clean": "clean"}.get(kind, "error")
        with (
            patch.object(svc, "get_settings", return_value=_settings()),
            patch.object(svc, "scan_claimed", AsyncMock(return_value=ScanResult(kind))),
            patch.object(svc, "_in_session", AsyncMock(return_value=state)),
            patch.object(svc, "_notify_uploader") as notify,
        ):
            await svc._scan_and_record(_claim(), MagicMock())
        assert notify.called is mailed
