"""Seeded workspaces, members and connectors for the connector-key tests.

Shared by the #2305 erasure tests and the #2352 member-removal / admin-delete /
``cli.delete_admin`` tests: whatever deletes a user's ``api_keys`` rows must
hand the live connector write keys to the workspace's owner and leave every
other row alone. The #2373 tests add agents: their keys die with the service
member, their registrar's departure names the owner. The ``connector_world``
fixture (``conftest.py``) yields a ``_ConnectorWorld``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.agent import Agent, AgentContextBinding
from models.auth import APIKey, AuditLog, Context, User, Workspace, WorkspaceMember
from models.erasure import REASON_USER_REQUEST_VIA_SUPPORT, STATUS_COMPLETE, ErasureRequest
from services.account_erasure_service import AccountErasureService, _audit_salt
from services.connector_key_ownership import DEPARTED_MEMBER_CREDENTIALS_ACTION
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow
from utils.hashing import sha256_hex


class _ConnectorWorld:
    """Seeds workspaces, members and connectors for the #2305 / #2352 tests and
    remembers what to clean up (the database outlives a test)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.workspace_ids: list[UUID] = []
        self.targets: list[str] = []

    async def user(self, prefix: str, *, verified: bool = False) -> str:
        """A ``users`` row (only the erased subjects need one). ``verified``:
        its address is verified, as an addressed invitation's accepter's must
        be (#2785)."""
        user_id = f"{prefix}_{uuid4().hex[:8]}"
        self.db.add(
            User(
                email=f"{user_id}@example.com",
                user_id=user_id,
                name=prefix,
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
                email_verified_at=utcnow() if verified else None,
            )
        )
        await self.db.flush()
        self.targets.append(user_id)
        return user_id

    async def workspace(self, owner: str, **members: str) -> Workspace:
        """A workspace ``owner`` owns; ``members`` maps user_id → role."""
        # Production ownership writers require live accounts; seed real users
        # for the convenience ids accepted by this fixture.
        for user_id in {owner, *members}:
            if await self.db.scalar(select(User.user_id).where(User.user_id == user_id)) is None:
                self.db.add(User(user_id=user_id, email=f"{user_id}@example.test"))
                self.targets.append(user_id)
        await self.db.flush()
        workspace = Workspace(
            id=uuid4(),
            name=f"ws-{uuid4().hex[:8]}",
            plan_name="promax",  # #1551: provisioning creates, XL only
            owner_user_id=owner,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
        self.db.add(workspace)
        await self.db.flush()
        self.workspace_ids.append(workspace.id)
        self.db.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=owner, role=WorkspaceRole.OWNER)
        )
        for user_id, role in members.items():
            self.db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role=role))
        await self.db.flush()
        return workspace

    async def connector(self, workspace: Workspace, by: str):
        """A connector ``by`` provisions, with its KMC write key."""
        from services.connector_provisioning import ConnectorProvisioningService

        result = await ConnectorProvisioningService(self.db).provision_connector(
            workspace_id=workspace.id,
            user_id=by,
            connector_type="slack",
            resource_id=f"slack_{uuid4().hex[:8]}",
            auto_create_context_name=f"slack-{uuid4().hex[:8]}",
        )
        await self.db.flush()
        return result

    async def personal_key(self, user_id: str, workspace: Workspace) -> UUID:
        from auth.api_keys import APIKeyManager

        _, row = await APIKeyManager(self.db).create_key(
            name=f"personal-{uuid4().hex[:6]}", user_id=user_id, workspace_id=workspace.id
        )
        await self.db.flush()
        return row.id

    async def agent(self, workspace: Workspace, by: str) -> Agent:
        """An agent ``by`` registers, bound to a context of the workspace's
        owner (one that outlives ``by``, #2373)."""
        agent = Agent(workspace_id=workspace.id, name=f"agent-{uuid4().hex[:8]}", owner_user_id=by)
        context = Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=workspace.owner_user_id,
        )
        self.db.add_all([agent, context])
        await self.db.flush()
        self.db.add(
            AgentContextBinding(
                agent_id=agent.id, context_id=context.id, is_default=True, created_by=by
            )
        )
        await self.db.flush()
        return agent

    async def agent_key(self, user_id: str, workspace: Workspace, agent: Agent) -> tuple[str, int]:
        """A live key bound to ``agent`` for the service member ``user_id``,
        as the owner mint creates it; returns (plaintext, id)."""
        from auth.api_keys import APIKeyManager

        plaintext, row = await APIKeyManager(self.db).create_key(
            name=f"agent-key-{uuid4().hex[:6]}",
            user_id=user_id,
            workspace_id=workspace.id,
            agent_id=agent.id,
        )
        await self.db.flush()
        return plaintext, row.id

    async def agent_state(self, agent_id: UUID) -> tuple[str, str, int]:
        """(owner_user_id, status, bindings) of the agent, read fresh."""
        owner, status = (
            await self.db.execute(
                select(Agent.owner_user_id, Agent.status).where(Agent.id == agent_id)
            )
        ).one()
        bindings = await self.db.scalar(
            select(func.count())
            .select_from(AgentContextBinding)
            .where(AgentContextBinding.agent_id == agent_id)
        )
        return owner, status, bindings or 0

    async def departure_audits(self, departing: str) -> list[AuditLog]:
        """The ``departed_member_credentials`` rows naming ``departing``."""
        from config.settings import get_settings
        from utils.hashing import hmac_sha256_hex

        return list(
            (
                await self.db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action == DEPARTED_MEMBER_CREDENTIALS_ACTION,
                        AuditLog.new_value_hash
                        == hmac_sha256_hex(departing, get_settings().audit_hmac_key),
                    )
                    .order_by(AuditLog.id)
                )
            ).scalars()
        )

    async def erase(self, target: str) -> dict:
        """Force-erase ``target``; returns the summary's ``postgres`` counts."""
        await self.db.commit()
        request = await AccountErasureService(self.db).admin_force_erase(
            target_user_id=target,
            initiator_user_id="admin-runner",
            reason_code=REASON_USER_REQUEST_VIA_SUPPORT,
            reason_detail="integration test",
        )
        assert request.status == STATUS_COMPLETE
        counts = request.deleted_data_summary["postgres"]
        # The re-own is a Core UPDATE; reload rather than read stale ORM state.
        self.db.expire_all()
        return counts

    async def remove(self, workspace_id: UUID, user_id: str) -> None:
        """Remove ``user_id`` from the workspace the way the route does (#2352)."""
        await self.db.commit()
        await WorkspaceService(self.db).remove_member(workspace_id, user_id)
        # The re-own is a Core UPDATE; reload rather than read stale ORM state.
        self.db.expire_all()

    async def cleanup(self) -> None:
        await self.db.rollback()
        pseudonyms = [sha256_hex(t, salt=_audit_salt()) for t in self.targets]
        await self.db.execute(
            delete(AuditLog).where(
                AuditLog.action.in_(("account_erasure", "account_erasure_reprocess"))
                & AuditLog.user_id.in_(pseudonyms)
            )
        )
        await self.db.execute(
            delete(AuditLog).where(
                (AuditLog.action == DEPARTED_MEMBER_CREDENTIALS_ACTION)
                & AuditLog.user_id.in_(self.targets)
            )
        )
        await self.db.execute(
            delete(ErasureRequest).where(ErasureRequest.user_id.in_(self.targets + pseudonyms))
        )
        await self.db.execute(delete(Workspace).where(Workspace.id.in_(self.workspace_ids)))
        await self.db.execute(delete(User).where(User.user_id.in_(self.targets)))
        await self.db.commit()


async def _key_rows(db_session: AsyncSession, ids) -> dict:
    """Byte-level ``to_jsonb`` of the ``api_keys`` rows ``ids``."""
    rows = (
        await db_session.execute(
            text("SELECT t.id, to_jsonb(t) FROM api_keys AS t WHERE t.id = ANY(:ids)"),
            {"ids": list(ids)},
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _live_connector_key(db_session: AsyncSession, connector_id) -> APIKey:
    return (
        await db_session.execute(
            select(APIKey).where(APIKey.connector_id == connector_id, APIKey.revoked_at.is_(None))
        )
    ).scalar_one()


def _without_owner(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "user_id"}
