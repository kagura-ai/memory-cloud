"""Seeded workspaces, members and connectors for the connector-key tests.

Shared by the #2305 erasure tests and the #2352 member-removal / admin-delete /
``cli.delete_admin`` tests: whatever deletes a user's ``api_keys`` rows must
hand the live connector write keys to the workspace's owner and leave every
other row alone. The ``connector_world`` fixture (``conftest.py``) yields a
``_ConnectorWorld``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import APIKey, AuditLog, User, Workspace, WorkspaceMember
from models.erasure import REASON_USER_REQUEST_VIA_SUPPORT, STATUS_COMPLETE, ErasureRequest
from services.account_erasure_service import AccountErasureService, _audit_salt
from services.workspace_service import WorkspaceService
from utils.hashing import sha256_hex


class _ConnectorWorld:
    """Seeds workspaces, members and connectors for the #2305 / #2352 tests and
    remembers what to clean up (the database outlives a test)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.workspace_ids: list[UUID] = []
        self.targets: list[str] = []

    async def user(self, prefix: str) -> str:
        """A ``users`` row (only the erased subjects need one)."""
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
