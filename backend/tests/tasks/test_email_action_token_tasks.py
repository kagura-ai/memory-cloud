"""Hourly cleanup of used and expired email action tokens (#1738)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from models.auth import EmailActionToken, User
from tasks import email_action_token_tasks
from tasks.email_action_token_tasks import (
    cleanup_email_action_tokens,
    schedule_email_action_token_tasks,
)
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow

RETENTION = 86400


def _settings(retention: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tasks.email_action_token_tasks.get_settings",
        lambda: Settings(_env_file=None, email_action_token_retention_seconds=retention),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(db_session: AsyncSession) -> AsyncIterator[str]:
    uid = f"u_{uuid4().hex[:10]}"
    db_session.add(
        User(
            user_id=uid,
            email=f"{uid}@tokens.example",
            name="Tokens",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
        )
    )
    await db_session.commit()
    yield uid
    await db_session.rollback()
    # Tokens cascade with the user.
    await db_session.execute(delete(User).where(User.user_id == uid))
    await db_session.commit()


def _token(user_id: str, name: str, *, expires_at, used_at=None) -> EmailActionToken:
    return EmailActionToken(
        user_id=user_id,
        purpose="reset_password",
        token_hash=f"{name}-{uuid4().hex}".ljust(64, "0")[:64],
        email=f"{user_id}@tokens.example",
        expires_at=expires_at,
        used_at=used_at,
    )


class TestCleanup:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_deletes_rows_used_or_expired_before_the_retention_window(
        self, db_session: AsyncSession, user_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(RETENTION, monkeypatch)
        now = utcnow()
        old, recent, future = (
            now - timedelta(days=2),
            now - timedelta(hours=1),
            now + timedelta(minutes=30),
        )
        rows = {
            "live": _token(user_id, "live", expires_at=future),
            "used-recently": _token(user_id, "used-recently", expires_at=future, used_at=recent),
            "used-long-ago": _token(user_id, "used-long-ago", expires_at=future, used_at=old),
            "expired-recently": _token(user_id, "expired-recently", expires_at=recent),
            "expired-long-ago": _token(user_id, "expired-long-ago", expires_at=old),
        }
        db_session.add_all(rows.values())
        await db_session.commit()
        hashes = {name: row.token_hash for name, row in rows.items()}

        deleted = await cleanup_email_action_tokens(db_session, now=now)
        await db_session.commit()

        left = set(
            (
                await db_session.execute(
                    select(EmailActionToken.token_hash).where(EmailActionToken.user_id == user_id)
                )
            ).scalars()
        )
        assert deleted >= 2
        assert left == {hashes["live"], hashes["used-recently"], hashes["expired-recently"]}

    @pytest.mark.asyncio(loop_scope="session")
    async def test_zero_retention_deletes_as_soon_as_used_or_expired(
        self, db_session: AsyncSession, user_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(0, monkeypatch)
        now = utcnow()
        live = _token(user_id, "live", expires_at=now + timedelta(minutes=30))
        used = _token(
            user_id,
            "used",
            expires_at=now + timedelta(minutes=30),
            used_at=now - timedelta(seconds=1),
        )
        db_session.add_all([live, used])
        await db_session.commit()
        live_hash = live.token_hash

        await cleanup_email_action_tokens(db_session, now=now)
        await db_session.commit()

        left = set(
            (
                await db_session.execute(
                    select(EmailActionToken.token_hash).where(EmailActionToken.user_id == user_id)
                )
            ).scalars()
        )
        assert left == {live_hash}


class TestCleanupRun:
    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_commits_and_logs_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(RETENTION, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=3))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(email_action_token_tasks, "get_db", mock_get_db_factory(db))
        mock_logger = MagicMock()
        monkeypatch.setattr(email_action_token_tasks, "logger", mock_logger)

        await email_action_token_tasks.cleanup_email_action_tokens_task()

        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        mock_logger.info.assert_called_once_with("email_action_token_cleanup_completed", deleted=3)

    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_rolls_back_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(RETENTION, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(email_action_token_tasks, "get_db", mock_get_db_factory(db))
        mock_logger = MagicMock()
        monkeypatch.setattr(email_action_token_tasks, "logger", mock_logger)

        await email_action_token_tasks.cleanup_email_action_tokens_task()  # must not raise

        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
        mock_logger.exception.assert_called_once_with("email_action_token_cleanup_failed")


class TestSchedulerRegistration:
    def test_registers_hourly_job(self) -> None:
        scheduler = MagicMock()
        schedule_email_action_token_tasks(scheduler)

        assert scheduler.add_job.call_count == 1
        args, kwargs = scheduler.add_job.call_args
        assert args[0] is email_action_token_tasks.cleanup_email_action_tokens_task
        assert kwargs["id"] == "cleanup_email_action_tokens"
        assert kwargs["replace_existing"] is True

    def test_registered_at_app_startup(self) -> None:
        main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
        assert "schedule_email_action_token_tasks(scheduler)" in main_src
