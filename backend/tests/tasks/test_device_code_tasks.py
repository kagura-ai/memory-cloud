"""Hourly cleanup of expired device codes (#1656)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from models.auth import OAuth2Client, OAuth2DeviceCode
from tasks import device_code_tasks
from tasks.device_code_tasks import (
    cleanup_expired_device_codes,
    expired_device_codes_delete,
    schedule_device_code_tasks,
)
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow


def _settings(retention: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tasks.device_code_tasks.get_settings",
        lambda: Settings(_env_file=None, oauth_device_code_retention_seconds=retention),
    )


class TestDeleteStatement:
    """The DELETE keeps unexpired rows and rows inside the retention window.

    Runs the real statement against an in-memory SQLite copy of the two tables
    so the WHERE clause is exercised, not just inspected.
    """

    @pytest.fixture
    def session(self):
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        OAuth2Client.__table__.create(engine)
        OAuth2DeviceCode.__table__.create(engine)
        factory = sessionmaker(bind=engine)
        with factory() as db:
            db.add(
                OAuth2Client(
                    client_id="cleanup-test-cli",
                    client_secret_hash="",
                    client_name="Test CLI",
                    grant_types=["urn:ietf:params:oauth:grant-type:device_code"],
                    response_types=[],
                    scope="memory:read",
                    redirect_uris=[],
                    token_endpoint_auth_method="none",
                    provider="claude",
                )
            )
            db.commit()
            yield db
        engine.dispose()

    def _add(self, db, user_code: str, expires_at) -> None:
        db.add(
            OAuth2DeviceCode(
                device_code=f"device-{user_code}",
                user_code=user_code,
                client_id="cleanup-test-cli",
                expires_at=expires_at,
            )
        )

    def test_deletes_only_rows_past_the_retention_window(self, session):
        now = utcnow()
        retention = timedelta(hours=1)
        self._add(session, "PASTWIN1", now - timedelta(hours=2))  # past the window
        self._add(session, "PASTWIN2", now - timedelta(hours=1, seconds=1))  # just past
        self._add(session, "INWINDOW", now - timedelta(minutes=30))  # expired, inside window
        self._add(session, "UNEXPIRD", now + timedelta(minutes=5))  # still valid
        session.commit()

        result = session.execute(expired_device_codes_delete(now - retention))
        session.commit()

        assert result.rowcount == 2
        remaining = {row.user_code for row in session.query(OAuth2DeviceCode).all()}
        assert remaining == {"INWINDOW", "UNEXPIRD"}

    def test_second_run_deletes_nothing(self, session):
        now = utcnow()
        self._add(session, "PASTWIN1", now - timedelta(hours=2))
        session.commit()

        session.execute(expired_device_codes_delete(now - timedelta(hours=1)))
        session.commit()
        again = session.execute(expired_device_codes_delete(now - timedelta(hours=1)))

        assert again.rowcount == 0


class TestCleanupRun:
    @pytest.mark.asyncio
    async def test_cutoff_is_now_minus_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(3600, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=4))
        now = utcnow()

        deleted = await cleanup_expired_device_codes(db, now=now)

        assert deleted == 4
        stmt = db.execute.await_args.args[0]
        compiled = stmt.compile()
        assert "DELETE FROM oauth_device_codes" in str(compiled)
        assert "expires_at <" in str(compiled)
        assert list(compiled.params.values()) == [now - timedelta(seconds=3600)]

    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_commits_and_logs_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(3600, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=7))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(device_code_tasks, "get_db", mock_get_db_factory(db))
        mock_logger = MagicMock()
        monkeypatch.setattr(device_code_tasks, "logger", mock_logger)

        await device_code_tasks.cleanup_expired_device_codes_task()

        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        mock_logger.info.assert_called_once_with("device_code_cleanup_completed", deleted=7)

    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_rolls_back_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(3600, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(device_code_tasks, "get_db", mock_get_db_factory(db))
        mock_logger = MagicMock()
        monkeypatch.setattr(device_code_tasks, "logger", mock_logger)

        await device_code_tasks.cleanup_expired_device_codes_task()  # must not raise

        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
        mock_logger.exception.assert_called_once_with("device_code_cleanup_failed")


class TestSchedulerRegistration:
    def test_registers_hourly_job(self) -> None:
        scheduler = MagicMock()
        schedule_device_code_tasks(scheduler)

        assert scheduler.add_job.call_count == 1
        args, kwargs = scheduler.add_job.call_args
        assert args[0] is device_code_tasks.cleanup_expired_device_codes_task
        assert kwargs["id"] == "cleanup_expired_device_codes"
        assert kwargs["replace_existing"] is True

    def test_registered_at_app_startup(self) -> None:
        main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
        assert "schedule_device_code_tasks(scheduler)" in main_src
