"""``db.base.isolated_session`` (#1985 recipe, shared since #2019)."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from db.base import isolated_session


@pytest.mark.asyncio
async def test_binds_to_the_given_session_engine():
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/none")
    try:
        db = MagicMock(bind=engine)
        session = isolated_session(db)
        assert isinstance(session, AsyncSession)
        assert session.bind is engine and session is not db
        assert session.sync_session.expire_on_commit is False
        await session.close()
    finally:
        await engine.dispose()


def test_falls_back_to_the_app_factory_without_an_engine():
    sentinel = MagicMock()
    with patch("db.base._get_session_factory", return_value=MagicMock(return_value=sentinel)):
        assert isolated_session(MagicMock(bind=None)) is sentinel
