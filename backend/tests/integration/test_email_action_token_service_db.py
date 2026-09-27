"""Email action tokens against real Postgres (Issue #1678).

- only the SHA-256 digest is stored;
- issue → consume works once; a second consume, an expired, an unknown, a
  malformed and a wrong-purpose token all answer ``None``;
- issuing a new token for the same (user, purpose) invalidates the old one;
- ``invalidate`` kills the outstanding tokens of the named purposes only;
- N sessions racing one token: exactly one wins (atomic UPDATE ... RETURNING).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.auth import EmailActionToken, User
from services.email_action_token_service import EmailActionTokenService
from utils.datetime import utcnow
from utils.hashing import sha256_hex

_PARALLEL = 8


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(db_session: AsyncSession) -> AsyncIterator[str]:
    uid = f"u_{uuid4().hex[:10]}"
    db_session.add(
        User(
            email=f"{uid}@token.invalid",
            user_id=uid,
            name="Token Test",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
        )
    )
    await db_session.commit()
    yield uid
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.user_id == uid))  # CASCADE
    await db_session.commit()


async def _issue(db: AsyncSession, uid: str, purpose: str = "reset_password") -> str:
    issued = await EmailActionTokenService(db).issue(
        user_id=uid,
        email=f"{uid}@token.invalid",
        purpose=purpose,  # type: ignore[arg-type]
    )
    await db.commit()
    return issued.raw_token


@pytest.mark.asyncio(loop_scope="session")
class TestEmailActionTokenService:
    async def test_stores_only_the_digest(self, db_session: AsyncSession, user_id: str) -> None:
        raw = await _issue(db_session, user_id)
        rows = (
            await db_session.scalars(
                select(EmailActionToken).where(EmailActionToken.user_id == user_id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].token_hash == sha256_hex(raw)
        for value in (rows[0].token_hash, rows[0].email, rows[0].purpose):
            assert raw not in value
        ttl = rows[0].expires_at - rows[0].created_at
        assert timedelta(minutes=29) < ttl <= timedelta(minutes=31)

    async def test_consumes_once(self, db_session: AsyncSession, user_id: str) -> None:
        raw = await _issue(db_session, user_id)
        service = EmailActionTokenService(db_session)

        consumed = await service.consume(raw_token=raw, purpose="reset_password")
        await db_session.commit()
        assert consumed is not None
        assert consumed.user_id == user_id
        assert consumed.email == f"{user_id}@token.invalid"

        assert await service.consume(raw_token=raw, purpose="reset_password") is None

    async def test_wrong_purpose_does_not_match(
        self, db_session: AsyncSession, user_id: str
    ) -> None:
        raw = await _issue(db_session, user_id, purpose="set_password")
        service = EmailActionTokenService(db_session)
        assert await service.consume(raw_token=raw, purpose="reset_password") is None
        # Still usable for its own purpose.
        assert await service.consume(raw_token=raw, purpose="set_password") is not None
        await db_session.commit()

    async def test_expired_is_refused(self, db_session: AsyncSession, user_id: str) -> None:
        raw = await _issue(db_session, user_id)
        await db_session.execute(
            update(EmailActionToken)
            .where(EmailActionToken.token_hash == sha256_hex(raw))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db_session.commit()
        assert (
            await EmailActionTokenService(db_session).consume(
                raw_token=raw, purpose="reset_password"
            )
            is None
        )

    @pytest.mark.parametrize("raw", ["", "short", "x" * 500, "A" * 42 + "\udcff"])
    async def test_malformed_and_unknown_are_refused(
        self, db_session: AsyncSession, user_id: str, raw: str
    ) -> None:
        assert (
            await EmailActionTokenService(db_session).consume(
                raw_token=raw, purpose="reset_password"
            )
            is None
        )

    async def test_unknown_well_formed_is_refused(self, db_session: AsyncSession) -> None:
        assert (
            await EmailActionTokenService(db_session).consume(
                raw_token="A" * 43, purpose="reset_password"
            )
            is None
        )

    async def test_a_new_token_invalidates_the_outstanding_one(
        self, db_session: AsyncSession, user_id: str
    ) -> None:
        first = await _issue(db_session, user_id)
        second = await _issue(db_session, user_id)
        service = EmailActionTokenService(db_session)
        assert await service.consume(raw_token=first, purpose="reset_password") is None
        assert await service.consume(raw_token=second, purpose="reset_password") is not None
        await db_session.commit()

    async def test_other_purposes_stay_valid(self, db_session: AsyncSession, user_id: str) -> None:
        verify = await _issue(db_session, user_id, purpose="verify_email")
        await _issue(db_session, user_id, purpose="reset_password")
        assert (
            await EmailActionTokenService(db_session).consume(
                raw_token=verify, purpose="verify_email"
            )
            is not None
        )
        await db_session.commit()

    async def test_invalidate_kills_only_the_named_purposes(
        self, db_session: AsyncSession, user_id: str
    ) -> None:
        reset = await _issue(db_session, user_id, purpose="reset_password")
        setup = await _issue(db_session, user_id, purpose="set_password")
        verify = await _issue(db_session, user_id, purpose="verify_email")
        service = EmailActionTokenService(db_session)

        await service.invalidate(user_id=user_id, purposes=("reset_password", "set_password"))
        await db_session.commit()

        assert await service.consume(raw_token=reset, purpose="reset_password") is None
        assert await service.consume(raw_token=setup, purpose="set_password") is None
        assert await service.consume(raw_token=verify, purpose="verify_email") is not None
        await db_session.commit()

    async def test_racing_consumers_win_exactly_once(
        self, async_engine, db_session: AsyncSession, user_id: str
    ) -> None:
        raw = await _issue(db_session, user_id)
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        start = asyncio.Event()

        async def _consume() -> bool:
            async with session_maker() as session:
                await start.wait()
                consumed = await EmailActionTokenService(session).consume(
                    raw_token=raw, purpose="reset_password"
                )
                await session.commit()
                return consumed is not None

        tasks = [asyncio.create_task(_consume()) for _ in range(_PARALLEL)]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert sum(results) == 1
