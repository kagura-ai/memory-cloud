"""``GET /api/v1/invitations/pending`` lists the invitations addressed to the caller.

An invitation without an email (a legacy row, or one written outside the API)
is reachable by whoever holds its link; it is not listed to every signed-in
user. The handler runs against a real ``db_session``.
"""

import uuid
from datetime import timedelta

from api.routes.invitations import get_pending_invitations
from models.auth import User, Workspace, WorkspaceInvitation
from utils.datetime import utcnow


async def _workspace(db) -> tuple[User, Workspace]:
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
    )
    db.add(ws)
    await db.flush()
    return owner, ws


def _invitation(ws: Workspace, owner: User, email: str | None) -> WorkspaceInvitation:
    return WorkspaceInvitation(
        workspace_id=ws.id,
        token=uuid.uuid4().hex + uuid.uuid4().hex,
        email=email,
        role="member",
        invited_by=owner.user_id,
        expires_at=utcnow() + timedelta(days=1),
    )


async def test_email_less_invitation_is_not_listed_to_an_unrelated_caller(db_session):
    owner, ws = await _workspace(db_session)
    db_session.add(_invitation(ws, owner, None))
    await db_session.flush()
    caller = {"user_id": f"user-{uuid.uuid4()}", "email": f"y-{uuid.uuid4()}@example.com"}

    response = await get_pending_invitations(current_user=caller, db=db_session)

    assert response.count == 0
    assert response.pending_invitations == []


async def test_invitation_addressed_to_the_caller_is_listed(db_session):
    owner_a, ws_a = await _workspace(db_session)
    owner_b, ws_b = await _workspace(db_session)
    email = f"x-{uuid.uuid4()}@example.com"
    addressed = _invitation(ws_b, owner_b, email.upper())
    db_session.add_all([_invitation(ws_a, owner_a, None), addressed])
    await db_session.flush()
    caller = {"user_id": f"user-{uuid.uuid4()}", "email": email}

    response = await get_pending_invitations(current_user=caller, db=db_session)

    assert response.count == 1
    assert [item.id for item in response.pending_invitations] == [addressed.public_id]
    assert response.pending_invitations[0].workspace_id == str(ws_b.id)
