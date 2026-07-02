"""Coverage-focused tests for ``services.invitation_service``.

Issue #165 / #654: Workspace Invitation System.

These tests exercise ``InvitationService`` end-to-end against a real
``db_session`` (rows are created with unique UUIDs), covering:

- ``build_invitation_url`` (env override + fallback)
- ``create_invitation`` happy path + every reachable validation branch
  (missing email, workspace-not-found, no shared contexts, missing/invalid
  context ids, single-owner constraint, invalid expiry preset, duplicate
  member, duplicate pending invitation, expired duplicate is allowed)
- the courtesy email dispatch (stub injection, inviter-name resolution,
  best-effort guard swallowing provider failure)
- ``get_invitation`` (found + not-found)
- ``list_invitations`` (pending-only vs include-accepted ordering)
- ``accept_invitation`` happy path + already-accepted, expired, email
  mismatch, already-member, workspace-missing, quota-exceeded branches
- ``delete_invitation`` (found + not-found)
- ``cleanup_expired_invitations`` (scoped + global + nothing-to-do)
- ``get_pending_invitations_for_email`` (email match, NULL-email match,
  expired/accepted exclusion)

External email I/O is always stubbed — no network calls are made.
"""

import uuid
from datetime import timedelta

import pytest

from models.auth import (
    Context,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from services.invitation_service import InvitationService, build_invitation_url
from utils.datetime import utcnow
from utils.exceptions import NotFoundException, ValidationError

# ---------------------------------------------------------------------------
# Row builders (unique UUIDs / IDs to avoid collisions across parallel tests)
# ---------------------------------------------------------------------------


def _uid() -> str:
    return f"user-{uuid.uuid4()}"


async def _make_user(db, *, name: str | None = None, email: str | None = None) -> User:
    user = User(
        email=email or f"{uuid.uuid4()}@example.com",
        user_id=_uid(),
        name=name,
        role="user",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_workspace(db, *, owner_user_id: str, plan_name: str = "pro") -> Workspace:
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4()}",
        owner_user_id=owner_user_id,
        plan_name=plan_name,
    )
    db.add(ws)
    await db.flush()
    return ws


async def _make_context(
    db, *, workspace_id, is_private: bool = False, deleted: bool = False
) -> Context:
    ctx = Context(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=f"ctx-{uuid.uuid4().hex[:8]}",
        is_private=is_private,
        deleted_at=utcnow() if deleted else None,
    )
    db.add(ctx)
    await db.flush()
    return ctx


async def _make_member(db, *, workspace_id, user_id: str, role: str = "member") -> WorkspaceMember:
    member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
    db.add(member)
    await db.flush()
    return member


class _StubEmail:
    """Records the single send_workspace_invitation call for assertions."""

    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.calls: list[dict] = []

    async def send_workspace_invitation(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("provider boom")
        return True


def _service(db, email_service=None) -> InvitationService:
    return InvitationService(db, email_service=email_service or _StubEmail())


# ---------------------------------------------------------------------------
# build_invitation_url
# ---------------------------------------------------------------------------


class TestBuildInvitationUrl:
    """``build_invitation_url`` reads FRONTEND_URL with a localhost fallback."""

    def test_uses_frontend_url_env(self, monkeypatch):
        """When FRONTEND_URL is set, it is used as the base."""
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")
        assert build_invitation_url("tok123") == "https://app.example.com/invite/tok123"

    def test_falls_back_to_localhost(self, monkeypatch):
        """When FRONTEND_URL is unset, falls back to local dev origin."""
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        assert build_invitation_url("abc") == "http://localhost:3000/invite/abc"


# ---------------------------------------------------------------------------
# create_invitation
# ---------------------------------------------------------------------------


class TestCreateInvitation:
    """``create_invitation`` validation and persistence branches."""

    async def test_missing_email_raises(self, db_session):
        """Empty/whitespace email is rejected before any DB lookup."""
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="Email is required"):
            await svc.create_invitation(workspace_id=uuid.uuid4(), invited_by="x", email="   ")

    async def test_none_email_raises(self, db_session):
        """None email is also rejected."""
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="Email is required"):
            await svc.create_invitation(workspace_id=uuid.uuid4(), invited_by="x", email=None)

    async def test_workspace_not_found_raises(self, db_session):
        """Unknown workspace id raises NotFoundException."""
        svc = _service(db_session)
        missing = uuid.uuid4()
        with pytest.raises(NotFoundException, match=str(missing)):
            await svc.create_invitation(
                workspace_id=missing, invited_by="x", email="a@example.com", role="admin"
            )

    async def test_member_role_no_shared_contexts_raises(self, db_session):
        """member/viewer invite with no shared contexts is rejected."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        # Only a private + a deleted context exist → no shared contexts.
        await _make_context(db_session, workspace_id=ws.id, is_private=True)
        await _make_context(db_session, workspace_id=ws.id, deleted=True)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="No shared contexts"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="a@example.com",
                role="member",
            )

    async def test_member_role_missing_context_ids_raises(self, db_session):
        """member/viewer invite without allowed_context_ids is rejected."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        await _make_context(db_session, workspace_id=ws.id)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="At least one context"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="a@example.com",
                role="viewer",
                allowed_context_ids=[],
            )

    async def test_member_role_invalid_context_ids_raises(self, db_session):
        """An allowed_context_id that isn't a shared context is rejected."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        await _make_context(db_session, workspace_id=ws.id)  # valid shared ctx exists
        bogus = uuid.uuid4()
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="Invalid or private context"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="a@example.com",
                role="member",
                allowed_context_ids=[bogus],
            )

    async def test_member_role_happy_path_persists(self, db_session):
        """A valid member invite persists with token, role, contexts."""
        owner = await _make_user(db_session, name="Owner Name")
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        ctx = await _make_context(db_session, workspace_id=ws.id)
        stub = _StubEmail()
        svc = InvitationService(db_session, email_service=stub)

        inv = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="invitee@example.com",
            role="member",
            allowed_context_ids=[ctx.id],
        )

        assert inv.id is not None
        assert inv.token and len(inv.token) >= 20
        assert inv.role == "member"
        assert inv.email == "invitee@example.com"
        assert inv.allowed_context_ids == [ctx.id]
        assert inv.expires_at is None  # default = never
        # Courtesy email dispatched once with resolved inviter name.
        assert len(stub.calls) == 1
        assert stub.calls[0]["to_email"] == "invitee@example.com"
        assert stub.calls[0]["inviter_name"] == "Owner Name"
        assert stub.calls[0]["workspace_name"] == ws.name
        assert stub.calls[0]["accept_url"].endswith(f"/invite/{inv.token}")

    async def test_admin_role_skips_context_requirement(self, db_session):
        """admin role bypasses the shared-context requirement entirely."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        inv = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="admin@example.com",
            role="admin",
        )
        assert inv.role == "admin"
        assert inv.allowed_context_ids is None

    async def test_owner_role_with_existing_owner_raises(self, db_session):
        """#1166: owner invitations are rejected outright (existing owner)."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        await _make_member(db_session, workspace_id=ws.id, user_id=owner.user_id, role="owner")
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="ownership transfer"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="newowner@example.com",
                role="owner",
            )

    async def test_owner_role_rejected_even_without_existing_owner(self, db_session):
        """#1166: owner invitations are rejected even in a zero-owner state.

        Before #1166 this path SUCCEEDED (the #165 single-owner check only
        fired when an owner row existed), which was the escalation edge: an
        invitation minted in a corrupted zero-owner state — or racing
        transfer_ownership — granted the owner role on accept. The sanctioned
        owner-change path is the ownership-transfer flow.
        """
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="ownership transfer"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="owner@example.com",
                role="owner",
            )

    async def test_invalid_expires_in_days_raises(self, db_session):
        """An expiry not in EXPIRY_PRESETS is rejected."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="Invalid expires_in_days"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="a@example.com",
                role="admin",
                expires_in_days=5,
            )

    async def test_valid_expires_in_days_sets_future_expiry(self, db_session):
        """A valid expiry preset sets expires_at ~7 days out."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        before = utcnow()
        inv = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="a@example.com",
            role="admin",
            expires_in_days=7,
        )
        assert inv.expires_at is not None
        delta = inv.expires_at - before
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)

    async def test_duplicate_existing_member_raises(self, db_session):
        """Inviting an email already belonging to a member is rejected."""
        owner = await _make_user(db_session)
        member_user = await _make_user(db_session, email="taken@example.com")
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        await _make_member(
            db_session, workspace_id=ws.id, user_id=member_user.user_id, role="admin"
        )
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="already a member"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="TAKEN@example.com",  # case-insensitive match
                role="admin",
            )

    async def test_duplicate_pending_invitation_raises(self, db_session):
        """A second active invite for the same email is rejected."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="dup@example.com",
            role="admin",
        )
        with pytest.raises(ValidationError, match="Active invitation already exists"):
            await svc.create_invitation(
                workspace_id=ws.id,
                invited_by=owner.user_id,
                email="DUP@example.com",
                role="admin",
            )

    async def test_expired_pending_invitation_does_not_block(self, db_session):
        """An existing-but-expired invite does NOT block a new invite."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        # Seed an already-expired, unaccepted invitation directly.
        old = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email="reuse@example.com",
            role="admin",
            invited_by=owner.user_id,
            expires_at=utcnow() - timedelta(days=1),
        )
        db_session.add(old)
        await db_session.flush()

        svc = _service(db_session)
        inv = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="reuse@example.com",
            role="admin",
        )
        assert inv.id is not None and inv.id != old.id


class TestCreateInvitationEmailDispatch:
    """Courtesy email dispatch is best-effort and never breaks creation."""

    async def test_email_failure_does_not_break_creation(self, db_session):
        """A raising email provider is swallowed; invitation still returned."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        stub = _StubEmail(raises=True)
        svc = InvitationService(db_session, email_service=stub)
        inv = await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="a@example.com",
            role="admin",
        )
        assert inv.id is not None
        assert len(stub.calls) == 1  # attempted exactly once

    async def test_inviter_name_falls_back_to_email(self, db_session):
        """A user with no display name resolves to their email."""
        owner = await _make_user(db_session, name=None, email="noname@example.com")
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        stub = _StubEmail()
        svc = InvitationService(db_session, email_service=stub)
        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="a@example.com",
            role="admin",
        )
        assert stub.calls[0]["inviter_name"] == "noname@example.com"

    async def test_inviter_name_falls_back_to_generic_when_user_missing(self, db_session):
        """An unknown inviter id resolves to the generic admin label."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        stub = _StubEmail()
        svc = InvitationService(db_session, email_service=stub)
        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by="ghost-user-id",  # no matching User row
            email="a@example.com",
            role="admin",
        )
        assert stub.calls[0]["inviter_name"] == "A Kagura workspace admin"

    async def test_resolve_inviter_name_blank_name_falls_back_to_email(self, db_session):
        """Whitespace-only name falls back to email in _resolve_inviter_name."""
        owner = await _make_user(db_session, name="   ", email="blank@example.com")
        svc = _service(db_session)
        name = await svc._resolve_inviter_name(owner.user_id)
        assert name == "blank@example.com"

    async def test_email_dispatch_includes_expiry_iso(self, db_session):
        """When the invite expires, the ISO expiry is forwarded to the email."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        stub = _StubEmail()
        svc = InvitationService(db_session, email_service=stub)
        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="a@example.com",
            role="admin",
            expires_in_days=30,
        )
        iso = stub.calls[0]["expires_at_iso"]
        assert iso is not None and iso.endswith("Z")

    async def test_dispatch_uses_singleton_when_no_override(self, db_session, monkeypatch):
        """With no override, the module singleton is resolved at dispatch time."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        sentinel = _StubEmail()
        # Patch the lazily-imported singleton getter used inside dispatch.
        import services.invitation_service as mod

        monkeypatch.setattr(mod, "get_email_service", lambda: sentinel)
        svc = InvitationService(db_session)  # no override
        await svc.create_invitation(
            workspace_id=ws.id,
            invited_by=owner.user_id,
            email="a@example.com",
            role="admin",
        )
        assert len(sentinel.calls) == 1


# ---------------------------------------------------------------------------
# get_invitation
# ---------------------------------------------------------------------------


class TestGetInvitation:
    """``get_invitation`` returns the row or raises NotFound."""

    async def test_found(self, db_session):
        """Returns the invitation matching the token."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        created = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="a@example.com", role="admin"
        )
        fetched = await svc.get_invitation(created.token)
        assert fetched.id == created.id

    async def test_not_found_raises(self, db_session):
        """An unknown token raises NotFoundException."""
        svc = _service(db_session)
        with pytest.raises(NotFoundException, match="not found or invalid"):
            await svc.get_invitation("nonexistent-token-value")


# ---------------------------------------------------------------------------
# list_invitations
# ---------------------------------------------------------------------------


class TestListInvitations:
    """``list_invitations`` filters accepted rows by default."""

    async def test_pending_only_excludes_accepted(self, db_session):
        """Default call returns only pending invitations."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        pending = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="p@example.com", role="admin"
        )
        accepted = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="acc@example.com", role="admin"
        )
        accepted.accepted_at = utcnow()
        await db_session.flush()

        rows = await svc.list_invitations(ws.id)
        ids = {r.id for r in rows}
        assert pending.id in ids
        assert accepted.id not in ids

    async def test_include_accepted_returns_all(self, db_session):
        """include_accepted=True returns accepted rows too."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        a = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="a@example.com", role="admin"
        )
        b = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="b@example.com", role="admin"
        )
        b.accepted_at = utcnow()
        await db_session.flush()

        rows = await svc.list_invitations(ws.id, include_accepted=True)
        ids = {r.id for r in rows}
        assert {a.id, b.id} <= ids


# ---------------------------------------------------------------------------
# accept_invitation
# ---------------------------------------------------------------------------


class TestAcceptInvitation:
    """``accept_invitation`` validation + membership creation."""

    async def _seed_pending(self, db_session, *, email="invitee@example.com", role="member"):
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id, plan_name="pro")
        inv = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=email,
            role=role,
            invited_by=owner.user_id,
        )
        db_session.add(inv)
        await db_session.flush()
        return owner, ws, inv

    async def test_happy_path_creates_member(self, db_session):
        """Accepting a valid invite creates the membership and marks accepted."""
        owner, ws, inv = await self._seed_pending(db_session)
        accepter = await _make_user(db_session, email="invitee@example.com")
        svc = _service(db_session)

        workspace, member = await svc.accept_invitation(
            token=inv.token, user_id=accepter.user_id, user_email="invitee@example.com"
        )

        assert workspace.id == ws.id
        assert member.workspace_id == ws.id
        assert member.user_id == accepter.user_id
        assert member.role == inv.role
        assert inv.accepted_at is not None
        assert inv.accepted_by == accepter.user_id
        # user's current workspace updated to invited workspace
        assert accepter.current_workspace_id == ws.id

    async def test_email_case_insensitive_match(self, db_session):
        """Email match is case-insensitive."""
        owner, ws, inv = await self._seed_pending(db_session, email="Mixed@Example.com")
        accepter = await _make_user(db_session, email="mixed@example.com")
        svc = _service(db_session)
        _, member = await svc.accept_invitation(
            token=inv.token, user_id=accepter.user_id, user_email="MIXED@EXAMPLE.COM"
        )
        assert member.user_id == accepter.user_id

    async def test_invalid_token_raises_not_found(self, db_session):
        """An unknown token raises NotFoundException."""
        svc = _service(db_session)
        with pytest.raises(NotFoundException):
            await svc.accept_invitation(
                token="missing-token", user_id="u", user_email="x@example.com"
            )

    async def test_already_accepted_raises(self, db_session):
        """A re-used (already accepted) invite is rejected."""
        owner, ws, inv = await self._seed_pending(db_session)
        inv.accepted_at = utcnow()
        inv.accepted_by = "someone"
        await db_session.flush()
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="already been accepted"):
            await svc.accept_invitation(
                token=inv.token, user_id="u2", user_email="invitee@example.com"
            )

    async def test_expired_raises(self, db_session):
        """An expired invite is rejected."""
        owner, ws, inv = await self._seed_pending(db_session)
        inv.expires_at = utcnow() - timedelta(days=1)
        await db_session.flush()
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="has expired"):
            await svc.accept_invitation(
                token=inv.token, user_id="u3", user_email="invitee@example.com"
            )

    async def test_email_mismatch_raises(self, db_session):
        """A logged-in email different from the invite restriction is rejected."""
        owner, ws, inv = await self._seed_pending(db_session, email="only@example.com")
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="restricted to"):
            await svc.accept_invitation(
                token=inv.token, user_id="u4", user_email="other@example.com"
            )

    async def test_already_member_raises(self, db_session):
        """A user already in the workspace cannot accept again."""
        owner, ws, inv = await self._seed_pending(db_session)
        accepter = await _make_user(db_session, email="invitee@example.com")
        await _make_member(db_session, workspace_id=ws.id, user_id=accepter.user_id, role="member")
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="already a member"):
            await svc.accept_invitation(
                token=inv.token, user_id=accepter.user_id, user_email="invitee@example.com"
            )

    async def test_workspace_missing_raises_not_found(self, db_session, monkeypatch):
        """If the workspace row vanished, accept raises NotFound."""
        owner, ws, inv = await self._seed_pending(db_session)
        accepter = await _make_user(db_session, email="invitee@example.com")
        svc = _service(db_session)

        # Return an in-memory invitation pointed at a non-existent workspace so
        # the workspace SELECT yields None (the FK would block persisting this).
        ghost_ws_id = uuid.uuid4()
        detached = WorkspaceInvitation(
            workspace_id=ghost_ws_id,
            token=inv.token,
            email="invitee@example.com",
            role="member",
            invited_by=owner.user_id,
        )

        async def _return_detached(token):
            return detached

        monkeypatch.setattr(svc, "get_invitation", _return_detached)

        with pytest.raises(NotFoundException, match="Workspace not found"):
            await svc.accept_invitation(
                token=inv.token, user_id=accepter.user_id, user_email="invitee@example.com"
            )

    async def test_quota_exceeded_raises_validation(self, db_session, monkeypatch):
        """A QuotaExceededError from the quota service surfaces as ValidationError."""
        from services import quota_service as qs
        from utils.exceptions import QuotaExceededError

        owner, ws, inv = await self._seed_pending(db_session)
        accepter = await _make_user(db_session, email="invitee@example.com")
        svc = _service(db_session)

        async def _boom(self_, workspace_id, raise_on_exceeded=False):
            raise QuotaExceededError("Member limit reached")

        monkeypatch.setattr(qs.QuotaService, "check_member_quota", _boom)

        with pytest.raises(ValidationError, match="Cannot accept invitation"):
            await svc.accept_invitation(
                token=inv.token, user_id=accepter.user_id, user_email="invitee@example.com"
            )

    async def test_accept_owner_role_invitation_rejected(self, db_session):
        """#1166: a pending role=owner invitation is refused at accept.

        Defense in depth: create now rejects owner invitations, but rows
        minted before the fix (or via direct DB access) may still exist.
        Accept must not grant the owner role — the ownership-transfer flow
        is the only sanctioned path.
        """
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id, plan_name="pro")
        inv = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=None,
            role="owner",
            invited_by=owner.user_id,
        )
        db_session.add(inv)
        await db_session.flush()
        accepter = await _make_user(db_session)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="ownership transfer"):
            await svc.accept_invitation(
                token=inv.token, user_id=accepter.user_id, user_email="anyone@example.com"
            )

    async def test_accept_owner_role_rejected_before_email_check(self, db_session):
        """#1166 / PR #1169: the owner-policy rejection fires BEFORE the email check.

        A legacy owner invitation WITH an email restriction, presented by a
        wrong-email caller, must surface the owner-policy rejection — not the
        email-mismatch error, which would leak the restricted address for an
        invitation that is invalid by policy regardless of who presents it.
        """
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id, plan_name="pro")
        inv = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email="restricted@example.com",
            role="owner",
            invited_by=owner.user_id,
        )
        db_session.add(inv)
        await db_session.flush()
        accepter = await _make_user(db_session)
        svc = _service(db_session)
        with pytest.raises(ValidationError, match="ownership transfer") as exc_info:
            await svc.accept_invitation(
                token=inv.token, user_id=accepter.user_id, user_email="wrong@example.com"
            )
        # The restricted email must NOT appear in the error (no leak).
        assert "restricted@example.com" not in str(exc_info.value)

    async def test_accept_with_no_email_restriction(self, db_session):
        """An invitation with no email restriction can be accepted by anyone."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id, plan_name="pro")
        inv = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=None,
            role="member",
            invited_by=owner.user_id,
        )
        db_session.add(inv)
        await db_session.flush()
        accepter = await _make_user(db_session)
        svc = _service(db_session)
        _, member = await svc.accept_invitation(
            token=inv.token, user_id=accepter.user_id, user_email="anyone@example.com"
        )
        assert member.user_id == accepter.user_id


# ---------------------------------------------------------------------------
# delete_invitation
# ---------------------------------------------------------------------------


class TestDeleteInvitation:
    """``delete_invitation`` removes the row scoped to its workspace."""

    async def test_delete_found(self, db_session):
        """A matching invitation is deleted."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        inv = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="a@example.com", role="admin"
        )
        await svc.delete_invitation(inv.id, ws.id)
        with pytest.raises(NotFoundException):
            await svc.get_invitation(inv.token)

    async def test_delete_not_found_raises(self, db_session):
        """A non-existent invitation id raises NotFound."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        with pytest.raises(NotFoundException, match="Invitation not found"):
            await svc.delete_invitation(999999999, ws.id)

    async def test_delete_wrong_workspace_raises(self, db_session):
        """An invitation id under a different workspace is not deletable."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        other_ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        inv = await svc.create_invitation(
            workspace_id=ws.id, invited_by=owner.user_id, email="a@example.com", role="admin"
        )
        with pytest.raises(NotFoundException):
            await svc.delete_invitation(inv.id, other_ws.id)


# ---------------------------------------------------------------------------
# cleanup_expired_invitations
# ---------------------------------------------------------------------------


class TestCleanupExpiredInvitations:
    """``cleanup_expired_invitations`` removes expired, unaccepted invites."""

    async def _seed_expired(self, db_session, ws_id, owner_id):
        inv = WorkspaceInvitation(
            workspace_id=ws_id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=f"{uuid.uuid4()}@example.com",
            role="admin",
            invited_by=owner_id,
            expires_at=utcnow() - timedelta(days=1),
        )
        db_session.add(inv)
        await db_session.flush()
        return inv

    async def test_scoped_cleanup_counts_deleted(self, db_session):
        """Workspace-scoped cleanup deletes only that workspace's expired rows."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        other = await _make_workspace(db_session, owner_user_id=owner.user_id)
        e1 = await self._seed_expired(db_session, ws.id, owner.user_id)
        await self._seed_expired(db_session, ws.id, owner.user_id)
        await self._seed_expired(db_session, other.id, owner.user_id)

        svc = _service(db_session)
        count = await svc.cleanup_expired_invitations(workspace_id=ws.id)
        assert count == 2
        with pytest.raises(NotFoundException):
            await svc.get_invitation(e1.token)

    async def test_unscoped_cleanup_removes_all_expired(self, db_session):
        """Unscoped cleanup removes expired rows across workspaces."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        await self._seed_expired(db_session, ws.id, owner.user_id)
        svc = _service(db_session)
        count = await svc.cleanup_expired_invitations()
        assert count >= 1

    async def test_nothing_to_cleanup_returns_zero(self, db_session):
        """A workspace with no expired invites yields a 0 count."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        svc = _service(db_session)
        count = await svc.cleanup_expired_invitations(workspace_id=ws.id)
        assert count == 0

    async def test_accepted_expired_not_cleaned(self, db_session):
        """An expired-but-accepted invite is NOT removed by cleanup."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        inv = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email="acc@example.com",
            role="admin",
            invited_by=owner.user_id,
            expires_at=utcnow() - timedelta(days=1),
            accepted_at=utcnow() - timedelta(hours=1),
        )
        db_session.add(inv)
        await db_session.flush()
        svc = _service(db_session)
        count = await svc.cleanup_expired_invitations(workspace_id=ws.id)
        assert count == 0
        # still present
        assert (await svc.get_invitation(inv.token)).id == inv.id


# ---------------------------------------------------------------------------
# get_pending_invitations_for_email
# ---------------------------------------------------------------------------


class TestGetPendingInvitationsForEmail:
    """``get_pending_invitations_for_email`` matches email or NULL restriction."""

    async def test_matches_email_and_null_restriction(self, db_session):
        """Returns email-matched and no-restriction invites, excludes others."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        target = f"pending-{uuid.uuid4()}@example.com"

        matched = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=target.upper(),  # case-insensitive
            role="admin",
            invited_by=owner.user_id,
        )
        no_restriction = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=None,
            role="admin",
            invited_by=owner.user_id,
        )
        other_email = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email="someoneelse@example.com",
            role="admin",
            invited_by=owner.user_id,
        )
        db_session.add_all([matched, no_restriction, other_email])
        await db_session.flush()

        svc = _service(db_session)
        rows = await svc.get_pending_invitations_for_email(target)
        ids = {r.id for r in rows}
        assert matched.id in ids
        assert no_restriction.id in ids
        assert other_email.id not in ids

    async def test_excludes_accepted_and_expired(self, db_session):
        """Accepted and expired invites are excluded from the pending list."""
        owner = await _make_user(db_session)
        ws = await _make_workspace(db_session, owner_user_id=owner.user_id)
        target = f"x-{uuid.uuid4()}@example.com"

        accepted = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=target,
            role="admin",
            invited_by=owner.user_id,
            accepted_at=utcnow(),
        )
        expired = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=target,
            role="admin",
            invited_by=owner.user_id,
            expires_at=utcnow() - timedelta(days=1),
        )
        live = WorkspaceInvitation(
            workspace_id=ws.id,
            token=uuid.uuid4().hex + uuid.uuid4().hex,
            email=target,
            role="admin",
            invited_by=owner.user_id,
            expires_at=utcnow() + timedelta(days=1),
        )
        db_session.add_all([accepted, expired, live])
        await db_session.flush()

        svc = _service(db_session)
        rows = await svc.get_pending_invitations_for_email(target)
        ids = {r.id for r in rows}
        assert live.id in ids
        assert accepted.id not in ids
        assert expired.id not in ids


class TestInvitationSchemaOwnerRejection:
    """#1166: the request schema refuses role=owner at the validation layer.

    FastAPI surfaces this as a 422 on POST /workspaces/{id}/invitations —
    before any route or service code runs — pointing the caller at the
    ownership-transfer flow.
    """

    def test_role_owner_rejected(self):
        from pydantic import ValidationError as PydanticValidationError

        from models.schemas import WorkspaceInvitationCreate

        with pytest.raises(PydanticValidationError, match="ownership transfer"):
            WorkspaceInvitationCreate(email="x@example.com", role="owner")

    def test_non_owner_roles_accepted(self):
        from models.schemas import WorkspaceInvitationCreate

        for role in ("admin", "member", "viewer"):
            req = WorkspaceInvitationCreate(email="x@example.com", role=role)
            assert req.role == role
