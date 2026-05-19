"""SQLAlchemy database setup and session management.

Provides async SQLAlchemy engine and session factory for PostgreSQL.

Note:
    OAuth2 server (Authlib) requires synchronous database access.
    A separate sync engine and session maker are provided for OAuth2 operations only.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from utils.logger import get_logger
from utils.url_redact import redact_db_url

logger = get_logger(__name__)


# Keep ``Base.metadata.create_all`` and alembic-authored DDL in sync on
# auto-generated constraint names (issue #613). Without a convention,
# SQLAlchemy emits anonymous ``Column(unique=True)`` and inline
# ``ForeignKey(...)`` declarations, letting PostgreSQL pick defaults that
# diverge wherever an alembic migration assigns an explicit name.
#
# ``ix`` is required when ``uq`` is set: ``Column(unique=True)``
# instantiates BOTH an unnamed UniqueConstraint and an unnamed backing
# Index, and the Index needs a name token or SQLAlchemy raises
# ``AssertionError: name is not None`` at DDL compile time.
#
# ``ck`` and ``pk`` are intentionally omitted — every CHECK constraint
# is explicitly named, and PostgreSQL's ``<table>_pkey`` default already
# matches alembic-side primary keys.
NAMING_CONVENTION: dict[str, str] = {
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ix": "ix_%(column_0_label)s",
}


# SQLAlchemy Base for models
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Lazy initialization (to avoid import-time errors)
engine = None
async_session_factory = None
sync_engine = None
sync_session_factory = None


def _get_engine():
    """Get or create async engine (lazy initialization)."""
    global engine

    if engine is None:
        from config.database import DATABASE_URL

        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # Belt-and-braces against container TZ/PGTZ being dropped in
            # a future deploy — see .claude/rules/backend.md for the
            # three-layer UTC policy.
            connect_args={"server_settings": {"timezone": "UTC"}},
        )
        logger.info("database_engine_created", url=redact_db_url(DATABASE_URL))

    return engine


def _get_session_factory():
    """Get or create session factory (lazy initialization)."""
    global async_session_factory

    if async_session_factory is None:
        async_session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )

    return async_session_factory


def _get_sync_engine():
    """Get or create sync engine for OAuth2 (lazy initialization).

    Note:
        This is ONLY used by OAuth2 server (Authlib requirement).
        All other parts of the application use async engine.
    """
    global sync_engine

    if sync_engine is None:
        from config.database import DATABASE_URL

        # Convert async URL (postgresql+asyncpg) to sync URL (postgresql+psycopg2)
        sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

        sync_engine = create_engine(
            sync_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # psycopg2 equivalent — see async engine above.
            connect_args={"options": "-c timezone=utc"},
        )
        logger.info("sync_database_engine_created", note="OAuth2 only")

    return sync_engine


def _get_sync_session_factory():
    """Get or create sync session factory for OAuth2 (lazy initialization).

    Note:
        This is ONLY used by OAuth2 server (Authlib requirement).
        All other parts of the application use async sessions.
    """
    global sync_session_factory

    if sync_session_factory is None:
        sync_session_factory = sessionmaker(
            _get_sync_engine(),
            class_=Session,
            expire_on_commit=False,
        )

    return sync_session_factory


def get_sync_session() -> Session:
    """Get synchronous database session for OAuth2 operations.

    Returns:
        Session instance (must be closed manually)

    Example:
        >>> session = get_sync_session()
        >>> try:
        >>>     # OAuth2 operations
        >>>     session.commit()
        >>> except Exception:
        >>>     session.rollback()
        >>>     raise
        >>> finally:
        >>>     session.close()

    Note:
        This is ONLY for OAuth2 server (Authlib requirement).
        Use get_db() for all other database operations.
    """
    factory = _get_sync_session_factory()
    return factory()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions.

    Yields:
        AsyncSession instance

    Example:
        @app.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            return result.scalars().all()
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database (create tables).

    Note:
        In production, use Alembic migrations instead.
        This is mainly for development/testing.
    """
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_initialized", tables=list(Base.metadata.tables.keys()))


async def close_db() -> None:
    """Close database connections."""
    global engine
    if engine:
        await engine.dispose()
        logger.info("database_connections_closed")
