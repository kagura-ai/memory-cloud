"""Unit tests for ``TermsService`` and ``TERMS_VERSION`` (Issue #1665) — no database.

Pinned: empty ``TERMS_VERSION`` means disabled (no query at all), a recorded
acceptance writes one ``terms_acceptances`` row plus one ``terms.accepted``
audit row that names the version only, re-accepting the newest version writes
nothing, and the setting refuses a value that cannot ride a URL. The newest-row
ordering against real rows lives in
``tests/integration/test_terms_acceptance_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from config.settings import Settings, get_settings
from models.auth import AuditLog
from models.terms import TERMS_ACCEPTANCE_SOURCES, TermsAcceptance
from services.terms_service import (
    TERMS_ACCEPTED_ACTION,
    TermsService,
    current_terms_version,
)

VERSION = "2026-09"


def _db(latest: str | None) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=latest))
    )
    db.commit = AsyncMock()
    # The users-row lock (SELECT ... FOR UPDATE) finds the user.
    db.scalar = AsyncMock(return_value="u1")
    return db


@pytest.fixture
def terms_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "terms_version", VERSION)


@pytest.fixture
def terms_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "terms_version", "")


class TestCurrentVersion:
    def test_empty_means_disabled(self, terms_off) -> None:
        assert current_terms_version() is None

    def test_set(self, terms_on) -> None:
        assert current_terms_version() == VERSION


class TestAcceptanceRequired:
    @pytest.mark.asyncio
    async def test_disabled_never_queries(self, terms_off) -> None:
        db = _db(None)
        assert await TermsService(db).acceptance_required("u1") is False
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("latest", "required"), [(None, True), ("2025-01", True)])
    async def test_missing_or_stale(self, terms_on, latest, required) -> None:
        assert await TermsService(_db(latest)).acceptance_required("u1") is required

    @pytest.mark.asyncio
    async def test_current(self, terms_on) -> None:
        assert await TermsService(_db(VERSION)).acceptance_required("u1") is False

    @pytest.mark.asyncio
    async def test_latest_is_the_newest_row_for_that_user(self) -> None:
        db = _db(None)
        await TermsService(db).latest_version("u1")
        sql = str(
            db.execute.await_args.args[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "WHERE terms_acceptances.user_id = 'u1'" in sql
        assert "ORDER BY terms_acceptances.accepted_at DESC" in sql
        assert "LIMIT 1" in sql


class TestRecord:
    @pytest.mark.asyncio
    async def test_writes_the_row_and_an_audit_row_naming_the_version_only(self) -> None:
        db = _db("2025-01")

        result = await TermsService(db).record(
            user_id="u1",
            user_email="u@example.test",
            version=VERSION,
            source="reaccept",
            ip_address="203.0.113.7",
            user_agent="pytest",
        )

        assert result.recorded is True
        assert result.version == VERSION
        added = [c.args[0] for c in db.add.call_args_list]
        acceptance = next(a for a in added if isinstance(a, TermsAcceptance))
        audit = next(a for a in added if isinstance(a, AuditLog))
        assert (acceptance.user_id, acceptance.version, acceptance.source) == (
            "u1",
            VERSION,
            "reaccept",
        )
        assert audit.action == TERMS_ACCEPTED_ACTION == "terms.accepted"
        assert audit.resource == f"terms_acceptance:{acceptance.id}"
        assert audit.user_metadata == {"version": VERSION}
        assert (audit.user_id, audit.user_email) == ("u1", "u@example.test")
        assert (audit.ip_address, audit.user_agent) == ("203.0.113.7", "pytest")
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_acceptance(self) -> None:
        db = _db(None)
        result = await TermsService(db).record(
            user_id="u1", user_email="u@example.test", version=VERSION, source="login"
        )
        assert result.recorded is True
        assert db.add.call_count == 2

    @pytest.mark.asyncio
    async def test_already_the_newest_version_writes_nothing(self) -> None:
        db = _db(VERSION)
        result = await TermsService(db).record(
            user_id="u1", user_email="u@example.test", version=VERSION, source="login"
        )
        assert result.recorded is False
        db.add.assert_not_called()
        # The commit only ends the transaction that held the row lock.
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_and_insert_run_under_a_users_row_lock(self) -> None:
        db = _db(None)
        calls: list[str] = []
        db.scalar.side_effect = lambda *_a, **_k: calls.append("lock") or "u1"
        original_execute = db.execute

        async def _execute(*args, **kwargs):
            calls.append("latest")
            return await original_execute(*args, **kwargs)

        db.execute = AsyncMock(side_effect=_execute)

        await TermsService(db).record(
            user_id="u1", user_email="u@example.test", version=VERSION, source="login"
        )

        # The lock comes before the "already accepted?" read.
        assert calls[:2] == ["lock", "latest"]
        sql = str(
            db.scalar.await_args.args[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "FROM users" in sql
        assert "users.user_id = 'u1'" in sql
        assert sql.rstrip().endswith("FOR UPDATE")

    @pytest.mark.asyncio
    async def test_missing_user_is_not_found(self) -> None:
        from utils.exceptions import NotFoundException

        db = _db(None)
        db.scalar = AsyncMock(return_value=None)
        with pytest.raises(NotFoundException):
            await TermsService(db).record(
                user_id="gone", user_email="g@example.test", version=VERSION, source="reaccept"
            )
        db.add.assert_not_called()


class TestModel:
    def test_check_constraint_matches_the_source_vocabulary(self) -> None:
        check = next(
            c
            for c in TermsAcceptance.__table__.constraints
            if c.name == "valid_terms_acceptance_source"
        )
        listed = ", ".join(f"'{s}'" for s in TERMS_ACCEPTANCE_SOURCES)
        assert str(check.sqltext) == f"source IN ({listed})"

    def test_accepted_at_is_timezone_aware(self) -> None:
        assert TermsAcceptance.__table__.c.accepted_at.type.timezone is True

    def test_user_fk_cascades(self) -> None:
        (fk,) = TermsAcceptance.__table__.c.user_id.foreign_keys
        assert fk.target_fullname == "users.user_id"
        assert fk.ondelete == "CASCADE"


class TestSetting:
    @pytest.fixture
    def clean_env(self, monkeypatch):
        monkeypatch.delenv("TERMS_VERSION", raising=False)
        return monkeypatch

    def test_default_is_empty(self, clean_env) -> None:
        assert Settings(_env_file=None).terms_version == ""

    def test_read_from_env_and_trimmed(self, clean_env) -> None:
        clean_env.setenv("TERMS_VERSION", "  2026-09.1  ")
        assert Settings(_env_file=None).terms_version == "2026-09.1"

    def test_whitespace_only_is_disabled(self, clean_env) -> None:
        clean_env.setenv("TERMS_VERSION", "   ")
        assert Settings(_env_file=None).terms_version == ""

    @pytest.mark.parametrize("bad", ["has space", "a&b=c", "x" * 65, "ü"])
    def test_refuses_a_value_that_cannot_ride_a_url(self, clean_env, bad) -> None:
        clean_env.setenv("TERMS_VERSION", bad)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
