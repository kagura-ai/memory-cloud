"""Route-logic tests for the secret store API (#1128).

Calls the route functions directly (no ASGI client) with constructed auth
principals and the real service over a Postgres ``db_session`` — the same
direct-invocation style as ``tests/api/test_share_keys_routes.py``. Verifies
auth extraction, error mapping (ValueError→400, denied→403, not-found→404), and
that no response model ever carries a value the server should not expose.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import Request
from sqlalchemy import func, select, text

from api.routes.secrets import (
    PubkeyRegister,
    RevokeGrant,
    SecretFetch,
    SecretPut,
    approve_pubkey,
    delete_secret,
    fetch_secret,
    list_secrets,
    put_secret,
    register_pubkey,
    revoke_grant,
    verify_audit_chain,
)
from models.auth import Workspace
from models.secrets import (
    AUDIT_ACTION_DELETE,
    Secret,
    SecretAccessLog,
    SecretGrant,
    SecretVersion,
)
from services.secret_store_service import SecretStoreService, fingerprint_pubkey
from utils.exceptions import AuthorizationError, BadRequestError, NotFoundException

PUBKEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
CIPHERTEXT = "-----BEGIN AGE ENCRYPTED FILE-----\nopaque==\n-----END AGE ENCRYPTED FILE-----"


def _req() -> Request:
    """Minimal Starlette Request for direct route-function calls (audit req_meta)."""
    return Request(
        {"type": "http", "headers": [(b"user-agent", b"pytest")], "client": ("127.0.0.1", 0)}
    )


@pytest_asyncio.fixture(loop_scope="session")
async def ctx(db_session):
    """Fresh workspace + service + auth principals; cascade teardown."""
    ws_id = uuid.uuid4()
    db_session.add(Workspace(id=ws_id, name=f"sec-rt-{ws_id}", owner_user_id="owner-1"))
    await db_session.flush()
    svc = SecretStoreService(db_session)
    member = {"user_id": "alice", "current_workspace_id": ws_id}
    admin = {"user_id": "owner-1", "current_workspace_id": ws_id}
    owner = ("owner-1", ws_id)
    yield {
        "ws": ws_id,
        "svc": svc,
        "db": db_session,
        "member": member,
        "admin": admin,
        "owner": owner,
    }
    # Audit log is append-only (row UPDATE/DELETE + TRUNCATE triggers); drop the
    # triggers (IF EXISTS — harmless when absent) before TRUNCATE so cleanup works
    # whether or not the migration applied them to this DB.
    await db_session.rollback()
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log")
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log")
    )
    await db_session.execute(text("TRUNCATE secret_access_log"))
    await db_session.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": str(ws_id)})
    await db_session.commit()


async def test_register_returns_pending_pubkey(ctx):
    resp = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A, label="laptop"),
        ctx["member"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    assert resp.status == "pending"
    assert resp.identity_id == "alice"
    assert resp.fingerprint == fingerprint_pubkey(PUBKEY_A)


async def test_put_before_approve_is_400(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    with pytest.raises(BadRequestError):
        await put_secret(
            SecretPut(
                name="cf/token",
                ciphertext=CIPHERTEXT,
                recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
                grant_pubkey_ids=[pk.id],
            ),
            _req(),
            ctx["admin"],
            svc=ctx["svc"],
            db=ctx["db"],
        )


async def test_full_flow_register_approve_put_fetch(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    put = await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    assert put.version_number == 1

    val = await fetch_secret(SecretFetch(name="cf/token"), _req(), ctx["member"], svc=ctx["svc"])
    assert val.ciphertext == CIPHERTEXT
    assert val.alg == "age"


async def test_fetch_without_grant_is_403(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    bob = {"user_id": "bob", "current_workspace_id": ctx["ws"]}
    with pytest.raises(AuthorizationError):
        await fetch_secret(SecretFetch(name="cf/token"), _req(), bob, svc=ctx["svc"])


async def test_list_secrets_has_no_value_field(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    listing = await list_secrets(ctx["admin"], svc=ctx["svc"])
    assert len(listing) == 1
    dumped = listing[0].model_dump()
    assert "ciphertext" not in dumped
    assert CIPHERTEXT not in str(dumped)


async def test_revoke_grant_flags_rotation(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    resp = await revoke_grant(
        RevokeGrant(name="cf/token", recipient_pubkey_id=pk.id),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    assert resp.rotation_needed is True


async def test_verify_audit_chain_endpoint(ctx):
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    resp = await verify_audit_chain(ctx["admin"], svc=ctx["svc"])
    assert resp.valid is True
    assert resp.entries >= 3  # register, approve, put


async def _seed_secret(ctx, name: str = "cf/token"):
    """register → approve → put one secret granted to PUBKEY_A; return its pubkey row."""
    pk = await register_pubkey(
        PubkeyRegister(pubkey=PUBKEY_A), ctx["member"], svc=ctx["svc"], db=ctx["db"]
    )
    await approve_pubkey(pk.id, ctx["owner"], svc=ctx["svc"], db=ctx["db"])
    await put_secret(
        SecretPut(
            name=name,
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    return pk


async def test_delete_secret_cascades_and_preserves_audit(ctx):
    """Owner delete removes the secret + cascades versions/grants; audit survives.

    Covers the four #1153 invariants in one flow: cascade integrity (no orphan
    versions/grants), audit-chain continuity across the delete (verify still
    valid), a recorded ``delete`` action, and that the secret does not reappear.
    """
    await _seed_secret(ctx)
    secret_id = (
        await ctx["db"].execute(
            select(Secret.id).where(Secret.workspace_id == ctx["ws"], Secret.name == "cf/token")
        )
    ).scalar_one()

    await delete_secret("cf/token", _req(), ctx["owner"], svc=ctx["svc"], db=ctx["db"])

    # Secret gone from the listing (and does not reappear).
    listing = await list_secrets(ctx["admin"], svc=ctx["svc"])
    assert all(r.name != "cf/token" for r in listing)

    # Cascade: no orphan versions or grants for the deleted secret.
    ver_count = (
        await ctx["db"].execute(
            select(func.count())
            .select_from(SecretVersion)
            .where(SecretVersion.secret_id == secret_id)
        )
    ).scalar_one()
    grant_count = (
        await ctx["db"].execute(
            select(func.count()).select_from(SecretGrant).where(SecretGrant.secret_id == secret_id)
        )
    ).scalar_one()
    assert ver_count == 0
    assert grant_count == 0

    # Audit chain stays valid AND records the delete (history is FK-decoupled, so
    # the entry for the now-deleted secret survives and still verifies).
    resp = await verify_audit_chain(ctx["admin"], svc=ctx["svc"])
    assert resp.valid is True
    delete_entries = (
        await ctx["db"].execute(
            select(func.count())
            .select_from(SecretAccessLog)
            .where(
                SecretAccessLog.action == AUDIT_ACTION_DELETE,
                SecretAccessLog.secret_id == secret_id,
            )
        )
    ).scalar_one()
    assert delete_entries == 1


async def test_delete_nonexistent_secret_is_404(ctx):
    """Deleting a name that does not exist surfaces as 404 (NotFoundException).

    (Owner-only enforcement is pinned structurally in test_route_auth_gating_wiring,
    which asserts the route's dependency is WorkspaceOwner — the same strategy this
    direct-invocation suite uses for every endpoint's gating.)
    """
    with pytest.raises(NotFoundException):
        await delete_secret("does/not-exist", _req(), ctx["owner"], svc=ctx["svc"], db=ctx["db"])


async def test_delete_then_reput_same_name_succeeds(ctx):
    """After delete, the name is free: a fresh put starts a new version-1 secret.

    Also pins the #1153 scope boundary: delete is scoped to the secret — the
    recipient pubkey survives (still ``active``), so the re-put reuses it rather
    than re-registering (which would 400 as "already registered").
    """
    pk = await _seed_secret(ctx)
    await delete_secret("cf/token", _req(), ctx["owner"], svc=ctx["svc"], db=ctx["db"])

    # Pubkey was NOT cascaded by the secret delete — reuse the still-active grant target.
    put = await put_secret(
        SecretPut(
            name="cf/token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        ),
        _req(),
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    assert put.version_number == 1
    assert (await verify_audit_chain(ctx["admin"], svc=ctx["svc"])).valid is True


def test_route_auth_gating_wiring():
    """Each endpoint is wired to the correct workspace auth dependency (review W6).

    Static pin so a regression that, e.g., loosens approve_pubkey from owner-only
    to member is caught — the gating itself is the shared, separately-tested
    require_workspace_* dependency chain.
    """
    import inspect

    from api.routes import secrets as r
    from auth.dependencies import WorkspaceAdmin, WorkspaceMember, WorkspaceOwner

    def ann(fn, param):
        # eval_str resolves PEP 563 string annotations (the route module uses
        # `from __future__ import annotations`) back to the alias object.
        return inspect.signature(fn, eval_str=True).parameters[param].annotation

    # Owner-only: the pubkey trust gate (TOFU) + destructive secret delete (#1153).
    assert ann(r.approve_pubkey, "owner") is WorkspaceOwner
    assert ann(r.revoke_pubkey, "owner") is WorkspaceOwner
    assert ann(r.delete_secret, "owner") is WorkspaceOwner
    # Owner/admin: secret management.
    assert ann(r.put_secret, "user") is WorkspaceAdmin
    assert ann(r.list_secrets, "user") is WorkspaceAdmin
    assert ann(r.revoke_grant, "user") is WorkspaceAdmin
    assert ann(r.list_pubkeys, "user") is WorkspaceAdmin
    assert ann(r.verify_audit_chain, "user") is WorkspaceAdmin
    # Member: register own pubkey / list own / fetch (service grant check gates value).
    assert ann(r.register_pubkey, "user") is WorkspaceMember
    assert ann(r.list_my_pubkeys, "user") is WorkspaceMember
    assert ann(r.fetch_secret, "user") is WorkspaceMember
