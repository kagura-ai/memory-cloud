"""#1944: a connector's write key stops with the connector it serves.

#1939 pauses connector ingest when a workspace's ``max_connectors`` drops to 0
(REST/MCP ingest, the worker config endpoint, the indexer). The worker also
holds a workspace-scoped API key (named ``connector:{connector.id}``) for the
legacy ``remember`` fallback, and that key kept writing. ``verify_key`` now
resolves such a key to its connector row and refuses it (``None`` → 401) while
the workspace's connector seats are 0 — nothing stored, so re-subscribing
restores it. #1952: the link is ``api_keys.connector_id``, not the name — a key
whose name merely carries the prefix is an ordinary key, a link to a connector
outside the key's workspace is refused (fail closed), and the user-facing
create endpoints do not accept the prefix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager
from auth.connector_keys import CONNECTOR_KEY_PREFIX, connector_key_name
from models.auth import User, Workspace
from models.resource import Resource, WorkspaceConnector
from utils.hashing import sha256_hex

API_KEY = "kagura_connector_test"
NOW = datetime(2026, 10, 12, 12, 0, 0)
WORKSPACE_ID = uuid4()
CONNECTOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Name formatting (display only since #1952)
# ---------------------------------------------------------------------------


def test_connector_key_name():
    cid = uuid4()
    assert connector_key_name(cid) == f"{CONNECTOR_KEY_PREFIX}{cid}"


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


def _key_record(
    name: str,
    *,
    workspace_id: UUID | None = WORKSPACE_ID,
    connector_id: UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        user_id="owner-1",
        name=name,
        workspace_id=workspace_id,
        bound_context_id=None,
        agent_id=None,
        default_context_id=None,  # #2129
        connector_id=connector_id,  # #1952
        key_prefix="kagura_c",
        key_hash=sha256_hex(API_KEY),
        revoked_at=None,
        expires_at=None,
        last_used_at=None,
    )


def _connector_record() -> SimpleNamespace:
    """The connector's own write key: linked by ``connector_id`` (#1952)."""
    return _key_record(connector_key_name(CONNECTOR_ID), connector_id=CONNECTOR_ID)


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
        record = _connector_record()
        db = _db(record, _workspace(0))

        with patch("auth.api_keys.logger") as logger:
            assert await _verify(db) is None

        assert record.last_used_at is None
        db.flush.assert_not_awaited()
        logger.info.assert_called_once()
        assert logger.info.call_args.args[0] == "api_key_rejected_connectors_suspended"

    @pytest.mark.asyncio
    async def test_connector_key_on_a_plan_with_seats_is_valid(self) -> None:
        record = _connector_record()
        db = _db(record, _workspace(3))

        verified = await _verify(db)

        assert verified is not None
        assert verified.workspace_id == WORKSPACE_ID
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_a_name_with_the_prefix_but_no_link_is_ordinary(self) -> None:
        """#1952: the name decides nothing — ``connector:<uuid>`` with
        ``connector_id`` NULL is an ordinary workspace key even on Free, with
        no connector query and no connector flag."""
        record = _key_record(connector_key_name(CONNECTOR_ID))
        db = _db(record)

        verified = await _verify(db)

        assert verified is not None and verified.is_connector_key is False
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_a_link_to_another_workspaces_connector_is_refused(self) -> None:
        """#1952 (fail closed): ``connector_id`` set but the join through the
        key's own workspace finds no row — tampered data — refuses the key."""
        record = _connector_record()
        db = _db(record, None)

        with patch("auth.api_keys.logger") as logger:
            assert await _verify(db) is None

        assert record.last_used_at is None
        assert db.execute.await_count == 2
        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[0] == "api_key_rejected_connector_workspace_mismatch"
        logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_connector_key_issues_no_workspace_query(self) -> None:
        record = _key_record("my laptop")
        db = _db(record)

        assert await _verify(db) is not None
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_owner_scoped_key_with_the_prefix_is_not_resolved(self) -> None:
        """No link, no workspace scope → an ordinary key; no second query."""
        record = _key_record(connector_key_name(CONNECTOR_ID), workspace_id=None)
        db = _db(record)

        assert await _verify(db) is not None
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_a_link_without_a_workspace_is_refused_without_a_query(self) -> None:
        """``ck_api_keys_connector_requires_workspace`` rules this row out;
        verify refuses it anyway rather than treating it as a global key."""
        record = _key_record(
            connector_key_name(CONNECTOR_ID), workspace_id=None, connector_id=CONNECTOR_ID
        )
        db = _db(record)

        assert await _verify(db) is None
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_connector_gate_runs_after_revocation(self) -> None:
        """A revoked connector key is None for the usual reason, with no extra query."""
        record = _connector_record()
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
async def connector_key(
    db_session: AsyncSession, fernet_api_key_secret: str
) -> AsyncIterator[dict[str, object]]:
    """A Free workspace with one connector and its minted write key (#1952 link).

    ``create_key`` encrypts the plaintext-for-reveal with the global Fernet
    encryptor, so a test secret is set and the singleton reset around the test.
    """
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
        name=connector_key_name(connector.id),
        user_id=user_id,
        workspace_id=workspace_id,
        connector_id=connector.id,
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
        "user_id": user_id,
        "other_ws_id": other_ws_id,
        "other_connector": other_connector,
    }


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

    async def test_create_key_refuses_another_workspaces_connector(
        self, db_session, connector_key
    ) -> None:
        """#1952: the cross-table workspace equality is checked at mint."""
        manager: APIKeyManager = connector_key["manager"]  # type: ignore[assignment]
        workspace: Workspace = connector_key["workspace"]  # type: ignore[assignment]
        other: WorkspaceConnector = connector_key["other_connector"]  # type: ignore[assignment]

        with pytest.raises(ValueError, match="workspace mismatch"):
            await manager.create_key(
                name="cross-link",
                user_id=connector_key["user_id"],  # type: ignore[arg-type]
                workspace_id=workspace.id,
                connector_id=other.id,
            )
        with pytest.raises(ValueError, match="not found"):
            await manager.create_key(
                name="missing-link",
                user_id=connector_key["user_id"],  # type: ignore[arg-type]
                workspace_id=workspace.id,
                connector_id=uuid4(),
            )
        with pytest.raises(ValueError, match="workspace-scoped"):
            await manager.create_key(
                name="global-link",
                user_id=connector_key["user_id"],  # type: ignore[arg-type]
                connector_id=other.id,
            )

    async def test_a_tampered_cross_workspace_link_is_refused_at_verify(
        self, db_session, connector_key
    ) -> None:
        """#1952 (fail closed): a row linked to a connector outside its own
        workspace — only reachable by writing the table directly — is refused
        even on a plan with seats."""
        manager: APIKeyManager = connector_key["manager"]  # type: ignore[assignment]
        workspace: Workspace = connector_key["workspace"]  # type: ignore[assignment]
        other: WorkspaceConnector = connector_key["other_connector"]  # type: ignore[assignment]
        plaintext, row = await manager.create_key(
            name="tampered",
            user_id=connector_key["user_id"],  # type: ignore[arg-type]
            workspace_id=workspace.id,
        )
        row.connector_id = other.id
        await db_session.flush()

        workspace.plan_name = "pro"
        await db_session.flush()
        db_session.expire(workspace)
        workspace.__dict__.pop("_plan_tier", None)

        assert await manager.verify_key(plaintext) is None


class TestVerifiedKeyConnectorFlag:
    """``VerifiedKey.is_connector_key``: the MCP transport withholds the
    last-used guardrail digest from a connector's shared key, whose owner is
    not the person on the other end of the connector."""

    @pytest.mark.asyncio
    async def test_connector_key_is_flagged(self) -> None:
        record = _connector_record()
        db = _db(record, _workspace(3))

        verified = await _verify(db)

        assert verified is not None and verified.is_connector_key is True

    @pytest.mark.asyncio
    async def test_ordinary_key_is_not_flagged(self) -> None:
        record = _key_record("my laptop")
        db = _db(record)

        verified = await _verify(db)

        assert verified is not None and verified.is_connector_key is False
