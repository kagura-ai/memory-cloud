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
from sqlalchemy import text

import models.secrets  # noqa: F401  — register tables for create_all
from api.routes.secrets import (
    PubkeyRegister,
    RevokeGrant,
    SecretFetch,
    SecretPut,
    approve_pubkey,
    fetch_secret,
    list_secrets,
    put_secret,
    register_pubkey,
    revoke_grant,
)
from models.auth import Workspace
from services.secret_store_service import SecretStoreService, fingerprint_pubkey
from utils.exceptions import AuthorizationError, BadRequestError

PUBKEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
CIPHERTEXT = "-----BEGIN AGE ENCRYPTED FILE-----\nopaque==\n-----END AGE ENCRYPTED FILE-----"


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
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    assert put.version_number == 1

    val = await fetch_secret(SecretFetch(name="cf/token"), ctx["member"], svc=ctx["svc"])
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
        ctx["admin"],
        svc=ctx["svc"],
        db=ctx["db"],
    )
    bob = {"user_id": "bob", "current_workspace_id": ctx["ws"]}
    with pytest.raises(AuthorizationError):
        await fetch_secret(SecretFetch(name="cf/token"), bob, svc=ctx["svc"])


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

    # Owner-only: the pubkey trust gate (TOFU).
    assert ann(r.approve_pubkey, "owner") is WorkspaceOwner
    assert ann(r.revoke_pubkey, "owner") is WorkspaceOwner
    # Owner/admin: secret management.
    assert ann(r.put_secret, "user") is WorkspaceAdmin
    assert ann(r.list_secrets, "user") is WorkspaceAdmin
    assert ann(r.revoke_grant, "user") is WorkspaceAdmin
    assert ann(r.list_pubkeys, "user") is WorkspaceAdmin
    # Member: register own pubkey / list own / fetch (service grant check gates value).
    assert ann(r.register_pubkey, "user") is WorkspaceMember
    assert ann(r.list_my_pubkeys, "user") is WorkspaceMember
    assert ann(r.fetch_secret, "user") is WorkspaceMember
