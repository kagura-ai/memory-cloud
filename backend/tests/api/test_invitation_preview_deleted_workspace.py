"""``GET /api/v1/invitations/{token}`` for a deleted workspace (#2785).

An invitation whose workspace was deleted can never be accepted, so its
preview answers exactly like an unknown token (404, same detail) instead of
naming the workspace. A live workspace's invitation still previews. The
handler runs against a real ``db_session``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException

from api.routes.invitations import get_invitation_info
from models.auth import User, Workspace, WorkspaceInvitation
from utils.datetime import utcnow


async def _invitation(db, *, deleted: bool) -> WorkspaceInvitation:
    owner = User(
        email=f"owner-{uuid.uuid4()}@example.com",
        user_id=f"user-{uuid.uuid4()}",
        role="user",
    )
    db.add(owner)
    await db.flush()
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4()}",
        owner_user_id=owner.user_id,
        plan_name="pro",
        deleted_at=utcnow() if deleted else None,
    )
    db.add(ws)
    await db.flush()
    invitation = WorkspaceInvitation(
        workspace_id=ws.id,
        token=uuid.uuid4().hex + uuid.uuid4().hex,
        email=None,
        role="member",
        invited_by=owner.user_id,
        expires_at=utcnow() + timedelta(days=1),
    )
    db.add(invitation)
    await db.flush()
    return invitation


async def _refusal(db, token: str) -> tuple[int, object]:
    with pytest.raises(HTTPException) as info:
        await get_invitation_info(token, db=db)
    return info.value.status_code, info.value.detail


async def test_deleted_workspace_answers_like_an_unknown_token(db_session) -> None:
    invitation = await _invitation(db_session, deleted=True)

    deleted = await _refusal(db_session, invitation.token)
    unknown = await _refusal(db_session, uuid.uuid4().hex + uuid.uuid4().hex)

    assert deleted == unknown
    assert deleted[0] == 404


async def test_live_workspace_still_previews(db_session) -> None:
    invitation = await _invitation(db_session, deleted=False)

    info = await get_invitation_info(invitation.token, db=db_session)

    assert info["role"] == "member"
    assert info["email_restricted"] is False
