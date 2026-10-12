"""#1944: a connector's write key stops with the connector it serves.

#1939 pauses connector ingest when a workspace's ``max_connectors`` drops to 0
(REST/MCP ingest, the worker config endpoint, the indexer). The worker also
holds a workspace-scoped API key named ``connector:{connector.id}`` for the
legacy ``remember`` fallback, and that key kept writing. ``verify_key`` now
resolves such a key to its connector row and refuses it (``None`` → 401) while
the workspace's connector seats are 0 — nothing stored, so re-subscribing
restores it. Only a key that names a real connector of its own workspace is
treated as a connector key; a user-minted ``connector:junk`` is an ordinary
key, and the user-facing create endpoints no longer accept the prefix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager
from auth.connector_keys import (
    CONNECTOR_KEY_PREFIX,
    connector_id_from_key_name,
    connector_key_name,
)
from models.auth import User, Workspace
from models.resource import Resource, WorkspaceConnector
from utils.hashing import sha256_hex

API_KEY = "kagura_connector_test"
NOW = datetime(2026, 10, 12, 12, 0, 0)
WORKSPACE_ID = uuid4()
CONNECTOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------


def test_connector_id_from_key_name():
    cid = uuid4()
    assert connector_key_name(cid) == f"{CONNECTOR_KEY_PREFIX}{cid}"
    # The formatter provisioning uses and the parser verify_key uses round-trip.
    assert connector_id_from_key_name(connector_key_name(cid)) == cid
    assert connector_id_from_key_name("connector:not-a-uuid") is None
    # Non-canonical spellings UUID() would accept are not the contract.
    assert connector_id_from_key_name(f"connector:{{{cid}}}") is None
    assert connector_id_from_key_name(f"connector:urn:uuid:{cid}") is None
    assert connector_id_from_key_name(f"connector:{cid.hex}") is None
    assert connector_id_from_key_name(f"connector:{str(cid).upper()}") is None
    assert connector_id_from_key_name("my laptop") is None
    assert connector_id_from_key_name("") is None


# ---------------------------------------------------------------------------
# verify_key gate (mock DB — pins the call order and the no-query fast path)
# ---------------------------------------------------------------------------


def _result(value: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _db(*rows: object | None) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(r) for r in rows])
    db.flush = AsyncMock()
    return db


def _key_record(name: str, *, workspace_id: UUID | None = WORKSPACE_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        user_id="owner-1",
        name=name,
        workspace_id=workspace_id,
        bound_context_id=None,
        agent_id=None,
        key_prefix="kagura_c",
        key_hash=sha256_hex(API_KEY),
        revoked_at=None,
        expires_at=None,
        last_used_at=None,
    )


def _workspace(max_connectors: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=WORKSPACE_ID, plan_name="free", effective_max_connectors=max_connectors
    )


async def _verify(db: MagicMock):
    fake_settings = SimpleNamespace(api_key_last_used_throttle_seconds=60)
    with (
        patch("auth.api_keys.utcnow", return_value=NOW),
        patch("auth.api_keys.get_settings", return_value=fake_settings),
    ):
        return await APIKeyManager(db).verify_key(API_KEY)


class TestVerifyKeyConnectorGate:
    @pytest.mark.asyncio
    async def test_suspended_connector_key_is_refused(self) -> None:
        """Key → its connector's workspace with 0 seats → None, and the refused
        attempt is not recorded as usage."""
        record = _key_record(f"connector:{CONNECTOR_ID}")
        db = _db(record, _workspace(0))

        with patch("auth.api_keys.logger") as logger:
            assert await _verify(db) is None

        assert record.last_used_at is None
        db.flush.assert_not_awaited()
        logger.info.assert_called_once()
        assert logger.info.call_args.args[0] == "api_key_rejected_connectors_suspended"

    @pytest.mark.asyncio
    async def test_connector_key_on_a_plan_with_seats_is_valid(self) -> None:
        record = _key_record(f"connector:{CONNECTOR_ID}")
        db = _db(record, _workspace(3))

        verified = await _verify(db)

        assert verified is not None
        assert verified.workspace_id == WORKSPACE_ID
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_user_key_that_only_looks_like_a_connector_key_is_ordinary(self) -> None:
        """``connector:<uuid>`` naming no connector of this workspace: the join
        finds no row, so the key is an ordinary workspace key even on Free."""
        record = _key_record(f"connector:{uuid4()}")
        db = _db(record, None)

        assert await _verify(db) is not None

    @pytest.mark.asyncio
    async def test_non_connector_key_issues_no_workspace_query(self) -> None:
        record = _key_record("my laptop")
        db = _db(record)

        assert await _verify(db) is not None
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_owner_scoped_key_with_the_prefix_is_not_resolved(self) -> None:
        """No workspace scope → nothing to pause; no second query."""
        record = _key_record(f"connector:{CONNECTOR_ID}", workspace_id=None)
        db = _db(record)

        assert await _verify(db) is not None
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_connector_gate_runs_before_the_agent_read(self) -> None:
        """An agent-bound connector key at 0 seats is refused before the Agent
        row is read — so no liveness touch (a write) happens for a refusal."""
        record = _key_record(f"connector:{CONNECTOR_ID}")
        record.agent_id = uuid4()
        db = _db(record, _workspace(0))

        assert await _verify(db) is None
        assert db.execute.await_count == 2  # key row, connector join — no Agent SELECT

    @pytest.mark.asyncio
    async def test_connector_gate_runs_after_revocation(self) -> None:
        """A revoked connector key is None for the usual reason, with no extra query."""
        record = _key_record(f"connector:{CONNECTOR_ID}")
        record.revoked_at = NOW
        db = _db(record)

        assert await _verify(db) is None
        assert db.execute.await_count == 1


# ---------------------------------------------------------------------------
# The prefix is reserved on the user-facing create endpoints
# ---------------------------------------------------------------------------


class TestReservedPrefix:
    def test_api_key_create_rejects_the_prefix(self) -> None:
        from api.routes.api_keys import APIKeyCreate

        with pytest.raises(ValidationError, match="reserved"):
            APIKeyCreate(name=f"connector:{uuid4()}")
        APIKeyCreate(name="connectors for my laptop")  # only the exact prefix

    def test_member_credentials_create_rejects_the_prefix(self) -> None:
        from models.schemas import CreateAPIKeyRequest

        with pytest.raises(ValidationError, match="reserved"):
            CreateAPIKeyRequest(name="connector:slack")
        CreateAPIKeyRequest(name="slack connector")


# ---------------------------------------------------------------------------
# Real DB: pause and resume with no stored state
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def connector_key(db_session: AsyncSession, monkeypatch) -> AsyncIterator[dict[str, object]]:
    """A Free workspace with one connector and its minted ``connector:{id}`` key.

    ``create_key`` encrypts the plaintext-for-reveal with the global Fernet
    encryptor, so a test secret is set and the singleton reset around the test.
    """
    import utils.encryption as enc_module

    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    enc_module._encryptor = None
    user_id = f"u_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    db_session.add(
        User(
            email=f"{user_id}@connector-pause.invalid",
            user_id=user_id,
            name="Connector Pause User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    workspace = Workspace(
        id=workspace_id,
        name=f"cpause-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=user_id,
        daily_api_limit=500,
        weekly_api_limit=2500,
    )
    db_session.add(workspace)
    resource_pk = uuid4()
    db_session.add(Resource(id=resource_pk, workspace_id=workspace_id, resource_id="conn-res"))
    await db_session.flush()
    connector = WorkspaceConnector(
        resource_pk=resource_pk,
        workspace_id=workspace_id,
        connector_type="slack",
        created_by=user_id,
    )
    db_session.add(connector)
    await db_session.flush()
    manager = APIKeyManager(db_session)
    plaintext, _row = await manager.create_key(
        name=f"connector:{connector.id}", user_id=user_id, workspace_id=workspace_id
    )
    user_plaintext, _row = await manager.create_key(
        name=f"connector:{uuid4()}", user_id=user_id, workspace_id=workspace_id
    )
    # A second Free workspace with its own connector: a key in the first
    # workspace that names THIS connector must stay an ordinary key (the join
    # is confined to the key's own workspace).
    other_ws_id = uuid4()
    db_session.add(
        Workspace(
            id=other_ws_id,
            name=f"cpause-other-{uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=user_id,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
    )
    other_resource_pk = uuid4()
    db_session.add(
        Resource(id=other_resource_pk, workspace_id=other_ws_id, resource_id="other-conn-res")
    )
    await db_session.flush()
    other_connector = WorkspaceConnector(
        resource_pk=other_resource_pk,
        workspace_id=other_ws_id,
        connector_type="slack",
        created_by=user_id,
    )
    db_session.add(other_connector)
    await db_session.flush()
    cross_plaintext, _row = await manager.create_key(
        name=connector_key_name(other_connector.id), user_id=user_id, workspace_id=workspace_id
    )
    yield {
        "workspace": workspace,
        "connector": connector,
        "plaintext": plaintext,
        "user_plaintext": user_plaintext,
        "cross_plaintext": cross_plaintext,
        "manager": manager,
    }
    enc_module._encryptor = None


@pytest.mark.asyncio(loop_scope="session")
class TestConnectorKeyPauseDB:
    async def test_free_refuses_and_a_paid_plan_resumes(self, db_session, connector_key) -> None:
        manager: APIKeyManager = connector_key["manager"]  # type: ignore[assignment]
        workspace: Workspace = connector_key["workspace"]  # type: ignore[assignment]

        assert await manager.verify_key(connector_key["plaintext"]) is None  # type: ignore[arg-type]

        workspace.plan_name = "pro"
        await db_session.flush()
        # One session, one identity-mapped row: drop the per-instance tier
        # cache a real request never carries across (each request loads anew).
        db_session.expire(workspace)
        workspace.__dict__.pop("_plan_tier", None)

        verified = await manager.verify_key(connector_key["plaintext"])  # type: ignore[arg-type]
        assert verified is not None
        assert verified.workspace_id == workspace.id

    async def test_a_key_naming_another_workspaces_connector_is_ordinary(
        self, db_session, connector_key
    ) -> None:
        manager: APIKeyManager = connector_key["manager"]  # type: ignore[assignment]
        assert await manager.verify_key(connector_key["cross_plaintext"]) is not None  # type: ignore[arg-type]

    async def test_a_look_alike_user_key_keeps_working_on_free(
        self, db_session, connector_key
    ) -> None:
        manager: APIKeyManager = connector_key["manager"]  # type: ignore[assignment]
        assert await manager.verify_key(connector_key["user_plaintext"]) is not None  # type: ignore[arg-type]
