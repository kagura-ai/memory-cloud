"""Alembic stays additive until an operator explicitly finalizes token storage."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import command
from auth.oauth_token_storage import finalize_oauth_token_storage, hash_oauth_token
from models.auth import OAuth2Client, OAuth2Token
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _downgrade_to,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

PRIOR = "f19_2391_embedding_erasure"
REVISION = "f20_2399_oauth_token_storage"


def test_empty_database_starts_bridge_retry_preserves_mode_and_downgrade_guards():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM oauth_tokens")).scalar_one() == 0
            assert (
                conn.execute(text("SELECT mode FROM oauth_token_storage WHERE id=1")).scalar_one()
                == "bridge"
            )
        # Pre-finalization schema rollback is safe and upgrading stays bridge.
        _downgrade_to(PRIOR)
        _upgrade_to(REVISION)
        with Session(engine) as db:
            assert finalize_oauth_token_storage(db, writers_drained=True) == 0
        # A retry after a lost revision stamp must never reset hashed to bridge.
        with _alembic_at_test_db():
            command.stamp(_get_alembic_config(), PRIOR)
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT mode FROM oauth_token_storage WHERE id=1")).scalar_one()
                == "hashed"
            )
            assert (
                conn.execute(
                    text(
                        "SELECT convalidated FROM pg_constraint WHERE conname='ck_oauth_tokens_hashed' AND conrelid='oauth_tokens'::regclass"
                    )
                ).scalar_one()
                is True
            )
        with pytest.raises(RuntimeError, match="downgrade.*unsafe"):
            _downgrade_to(PRIOR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == REVISION
            )
            assert (
                conn.execute(text("SELECT mode FROM oauth_token_storage WHERE id=1")).scalar_one()
                == "hashed"
            )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")


def test_populated_upgrade_preserves_legacy_reads_and_writes_until_finalized():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(PRIOR)
        with Session(engine) as db:
            db.add(
                OAuth2Client(
                    client_id="pre-hash-client",
                    client_secret_hash="",
                    client_name="Legacy test",
                    redirect_uris=[],
                    grant_types=["refresh_token"],
                    response_types=[],
                    scope="memory:read",
                    token_endpoint_auth_method="none",
                    provider="custom",
                )
            )
            db.flush()
            db.add(
                OAuth2Token(
                    client_id="pre-hash-client",
                    user_id="legacy-user",
                    access_token="pre-migration-access",
                    refresh_token="pre-migration-refresh",
                    expires_in=3600,
                )
            )
            db.commit()
        _upgrade_to(REVISION)
        with Session(engine) as db:
            assert (
                db.execute(text("SELECT mode FROM oauth_token_storage WHERE id=1")).scalar_one()
                == "bridge"
            )
            assert db.query(OAuth2Token).one().access_token == "pre-migration-access"
            # The deployment still has old writers when Alembic commits.
            db.add(
                OAuth2Token(
                    client_id="pre-hash-client",
                    user_id="legacy-user",
                    access_token="old-writer-after-migration",
                    refresh_token=None,
                    expires_in=3600,
                )
            )
            db.commit()
        with Session(engine) as db:
            assert finalize_oauth_token_storage(db, writers_drained=True) == 2
        with Session(engine) as db:
            row = (
                db.query(OAuth2Token)
                .filter_by(access_token=hash_oauth_token("pre-migration-access"))
                .one()
            )
            assert row.refresh_token == hash_oauth_token("pre-migration-refresh")
            assert (
                db.query(OAuth2Token)
                .filter_by(access_token=hash_oauth_token("old-writer-after-migration"))
                .one()
                .refresh_token
                is None
            )
    finally:
        engine.dispose()
        _reset_alembic_state()
        _upgrade_to("head")
