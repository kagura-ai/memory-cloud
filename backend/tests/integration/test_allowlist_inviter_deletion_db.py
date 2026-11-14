"""An erased or deleted account leaves no user id in the sign-up allowlist (#2814).

``signup_allowlist.added_by_user_id`` names who added a row: the admin who
allowlisted someone, or the user whose closed-beta invite link was redeemed.
When that person's account goes, the column is cleared, on the same paths as
``users.admitted_by_user_id`` (#2803):

- account erasure (``AccountErasureService._delete_postgres``);
- ``DELETE /admin/users/{id}`` (the handler, through ``delete_user_rows``);
- ``delete_user_rows`` and the ``delete_admin`` CLI's row deletion.

The row itself stays (it still admits the person it names), and rows added by
someone else keep their ``added_by_user_id``.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes.admin import delete_user
from cli.delete_admin import _delete_admin_rows
from models.auth import User
from models.signup_gate import SignupAllowlistEntry
from services.account_erasure_service import AccountErasureService
from services.user_deletion_service import delete_user_rows

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def rows(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    people: list[str] = []
    entries: list[UUID] = []

    async def person(**cols) -> str:
        user_id = cols.pop("user_id", _uid("p"))
        db_session.add(
            User(
                email=f"{user_id.replace(':', '-')}@example.test",
                user_id=user_id,
                role=cols.pop("role", "user"),
                auth_method=cols.pop("auth_method", "oauth"),
                auth_provider=cols.pop("auth_provider", "google"),
                **cols,
            )
        )
        await db_session.commit()
        people.append(user_id)
        return user_id

    async def entry(added_by: str | None, *, source: str = "beta_invite") -> UUID:
        entry_id = uuid4()
        sub = _uid("sub")
        db_session.add(
            SignupAllowlistEntry(
                id=entry_id,
                provider="google",
                subject_id=sub,
                subject_label=f"{sub}@example.test",
                github_user_id=f"google:{sub}",
                github_username=f"{sub}@example.test",
                source=source,
                state="active",
                added_by_user_id=added_by,
            )
        )
        await db_session.commit()
        entries.append(entry_id)
        return entry_id

    async def added_by(entry_id: UUID) -> tuple:
        row = (
            await db_session.execute(
                select(SignupAllowlistEntry.added_by_user_id, SignupAllowlistEntry.state)
                .where(SignupAllowlistEntry.id == entry_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return tuple(row) if row is not None else ()

    yield SimpleNamespace(person=person, entry=entry, added_by=added_by)
    await db_session.rollback()
    await db_session.execute(
        delete(SignupAllowlistEntry).where(SignupAllowlistEntry.id.in_(entries))
    )
    await db_session.execute(delete(User).where(User.user_id.in_(people)))
    await db_session.commit()


async def _setup(rows: SimpleNamespace, gone: str) -> tuple[UUID, UUID, UUID, str]:
    """Two rows the departing person added (an invite and a manual add), and
    one another admin added."""
    other = await rows.person(role="admin")
    invited = await rows.entry(gone)
    manual = await rows.entry(gone, source="manual")
    kept = await rows.entry(other, source="manual")
    return invited, manual, kept, other


async def _assert_cleared(
    rows: SimpleNamespace, invited: UUID, manual: UUID, kept: UUID, other: str
) -> None:
    assert await rows.added_by(invited) == (None, "active")
    assert await rows.added_by(manual) == (None, "active")
    assert await rows.added_by(kept) == (other, "active")


async def test_erasure_clears_the_inviter_from_the_allowlist(rows, maker) -> None:
    gone = await rows.person()
    invited, manual, kept, other = await _setup(rows, gone)

    async with maker() as s:
        target = await s.scalar(select(User).where(User.user_id == gone))
        erased = await AccountErasureService(s)._delete_postgres(target)

    await _assert_cleared(rows, invited, manual, kept, other)
    assert erased.counts["signup_allowlist_inviters_cleared"] == 2


async def test_admin_api_delete_clears_the_inviter_from_the_allowlist(rows, maker, vectors) -> None:
    gone = await rows.person(role="admin")
    invited, manual, kept, other = await _setup(rows, gone)

    async with maker() as s:
        await delete_user(gone, {"user_id": "operator"}, s)

    await _assert_cleared(rows, invited, manual, kept, other)


async def test_delete_user_rows_clears_the_inviter_from_the_allowlist(rows, maker, vectors) -> None:
    gone = await rows.person()
    invited, manual, kept, other = await _setup(rows, gone)

    async with maker() as s:
        await delete_user_rows(s, gone)
        await s.commit()

    await _assert_cleared(rows, invited, manual, kept, other)


async def test_delete_admin_cli_clears_the_inviter_from_the_allowlist(rows, maker, vectors) -> None:
    login = f"adm{uuid4().hex[:8]}"
    gone = await rows.person(
        user_id=f"local:{login}",
        login_id=login,
        role="admin",
        auth_method="password",
        auth_provider=None,
        admitted_via="admin_cli",
    )
    invited, manual, kept, other = await _setup(rows, gone)

    async with maker() as s:
        await _delete_admin_rows(s, gone, [])

    await _assert_cleared(rows, invited, manual, kept, other)
