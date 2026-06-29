"""Alembic environment configuration.

Async-compatible env.py for PostgreSQL + asyncpg.
Database URL is read from DATABASE_URL environment variable
(falls back to local dev default).
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import models.analysis  # noqa: F401  # Issue #494: Memory Broadlistening tables
import models.auth  # noqa: F401
import models.config  # noqa: F401
import models.erasure  # noqa: F401
import models.file_objects  # noqa: F401  # Issue #485: file storage tables
import models.hub_tag  # noqa: F401
import models.llm_pricing  # noqa: F401  # Issue #471: cost-grade pricing master
import models.memory  # noqa: F401
import models.neural  # noqa: F401
import models.resource  # noqa: F401
import models.secrets  # noqa: F401  # Issue #1128: zero-knowledge secret store
import models.sleep  # noqa: F401  # Issue #471: SleepReportLLMUsage child added
from alembic import context
from config.database import get_database_url

# Import all models so autogenerate can detect them
from db.base import Base  # noqa: F401

# Alembic Config object
config = context.config

# Set sqlalchemy.url dynamically from environment
# Ensure async driver (asyncpg) is used
db_url = get_database_url()
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
config.set_main_option("sqlalchemy.url", db_url)

# Setup Python logging from ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# MetaData for autogenerate support
target_metadata = Base.metadata

# Tables to exclude from autogenerate
EXCLUDE_TABLES: set[str] = set()


def include_object(object, name, type_, reflected, compare_to):  # noqa: A002
    """Filter objects for autogenerate."""
    if type_ == "table" and name in EXCLUDE_TABLES:
        return False
    return True


def include_name(name, type_, parent_names):
    """Filter names for autogenerate."""
    if type_ == "table" and name in EXCLUDE_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL. No Engine needed.
    Emits SQL to script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations with a given connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    # Pin the migration session timezone to UTC at the engine layer, parallel
    # to db.base._get_engine — without this, ``func.now()`` server defaults
    # in migrations would inherit the postgres server default (which container
    # TZ/PGTZ does NOT rewrite for an already-initialized data directory).
    # See .claude/rules/backend.md "Datetime / UTC" for the three-layer policy.
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
