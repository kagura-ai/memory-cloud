"""``TermsService`` against real Postgres (Issue #1665).

The unit suite mocks the session; here the rows are real:

- the newest row decides the accepted version, per user;
- ``record`` appends one row and one ``terms.accepted`` audit row naming the
  version only, and a repeat of the newest version writes neither;
- a version bump makes ``acceptance_required`` true again until re-accepted,
  while the older row stays as history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import AuditLog, User
from models.terms import TermsAcceptance
from services.terms_service import TERMS_ACCEPTED_ACTION, TermsService


def _new_user() -> User:
    user_id = f"u_{uuid4().hex[:10]}"
    return User(
        email=f"{user_id}@terms.invalid",
        user_id=user_id,
        name="Terms Test",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def people(db_session: AsyncSession) -> AsyncIterator[list[User]]:
    users = [_new_user(), _new_user()]
    db_session.add_all(users)
    await db_session.commit()
    yield users
    await db_session.rollback()
    ids = [u.user_id for u in users]
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id.in_(ids)))
    await db_session.execute(delete(User).where(User.user_id.in_(ids)))
    await db_session.commit()


async def _rows(db: AsyncSession, user_id: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(TermsAcceptance)
            .where(TermsAcceptance.user_id == user_id)
        )
    ).scalar_one()


@pytest.mark.asyncio(loop_scope="session")
async def test_record_history_and_reacceptance(
    db_session: AsyncSession, people: list[User], monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, bob = people
    service = TermsService(db_session)
    monkeypatch.setattr(get_settings(), "terms_version", "v1")

    assert await service.acceptance_required(alice.user_id) is True

    first = await service.record(
        user_id=alice.user_id, user_email=alice.email, version="v1", source="login"
    )
    assert first.recorded is True
    assert await service.latest_version(alice.user_id) == "v1"
    assert await service.acceptance_required(alice.user_id) is False
    # Per user: bob is untouched.
    assert await service.latest_version(bob.user_id) is None

    repeat = await service.record(
        user_id=alice.user_id, user_email=alice.email, version="v1", source="password"
    )
    assert repeat.recorded is False
    assert await _rows(db_session, alice.user_id) == 1

    # The operator bumps the version: asked again, the old row stays.
    monkeypatch.setattr(get_settings(), "terms_version", "v2")
    assert await service.acceptance_required(alice.user_id) is True
    await service.record(
        user_id=alice.user_id, user_email=alice.email, version="v2", source="reaccept"
    )
    assert await service.latest_version(alice.user_id) == "v2"
    assert await service.acceptance_required(alice.user_id) is False
    assert await _rows(db_session, alice.user_id) == 2

    audits = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.user_id == alice.user_id, AuditLog.action == TERMS_ACCEPTED_ACTION)
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert [a.user_metadata for a in audits] == [{"version": "v1"}, {"version": "v2"}]
    assert all(a.resource.startswith("terms_acceptance:") for a in audits)
