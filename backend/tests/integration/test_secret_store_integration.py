"""Integration tests for the zero-knowledge secret store (#1128).

Runs against a real PostgreSQL (the ``db_session`` fixture). Importing
``models.secrets`` here registers the tables with the shared metadata so
conftest's ``create_all`` builds them.

Note on the append-only trigger: conftest builds the schema with ``create_all``
(model metadata), which does NOT include the DB trigger (triggers are not part
of SQLAlchemy metadata). ``test_audit_log_append_only_trigger`` installs the
trigger via the same DDL the migration uses and asserts it blocks mutation —
proving the SQL the migration ships is correct.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from models.auth import Workspace
from models.secrets import (
    PUBKEY_STATUS_ACTIVE,
    PUBKEY_STATUS_PENDING,
    RecipientPubkey,
    Secret,
    SecretAccessLog,
    SecretGrant,
    SecretVersion,
)
from services.secret_store_service import (
    SecretAccessDenied,
    SecretNotFound,
    SecretStoreService,
    fingerprint_pubkey,
)

# asyncio_mode=auto (pyproject) auto-marks async tests; session loop scope is the
# configured default, so the session-scoped db_session/engine fixtures bind cleanly.

# Shape-valid age recipient keys (distinct). Not real crypto — the server only
# stores opaque ciphertext and never decrypts.
PUBKEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
PUBKEY_B = "age1lggyhqrw2nlhcxprm67z43rta597azn8gknawjehu9d9dl0jq3yqqvfafg"
CIPHERTEXT = (
    "-----BEGIN AGE ENCRYPTED FILE-----\nfakeopaqueciphertext==\n-----END AGE ENCRYPTED FILE-----"
)


async def _wipe_audit(db):
    """Clear the append-only audit table for cleanup.

    The migration installs a row UPDATE/DELETE trigger AND a statement TRUNCATE
    trigger; if either is present (e.g. the drift/migration test applied them to
    this DB), DELETE and TRUNCATE are both blocked. Drop them first (IF EXISTS —
    harmless when absent), then TRUNCATE.
    """
    await db.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log")
    )
    await db.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log")
    )
    await db.execute(text("TRUNCATE secret_access_log"))


@pytest_asyncio.fixture(loop_scope="session")
async def ws(db_session):
    """A fresh workspace per test; teardown cascades the secret tables."""
    workspace_id = uuid.uuid4()
    db_session.add(
        Workspace(id=workspace_id, name=f"sec-test-{workspace_id}", owner_user_id="owner-1")
    )
    await db_session.flush()
    yield workspace_id
    # Cleanup: get_secret() commits, so rows may be durable. Delete by workspace
    # (CASCADE removes pubkeys/secrets/versions/grants); the audit log is append-only.
    await db_session.rollback()
    await _wipe_audit(db_session)
    await db_session.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": str(workspace_id)})
    await db_session.commit()


async def _register_and_approve(svc: SecretStoreService, ws_id, identity: str, pubkey: str):
    pk = await svc.register_pubkey(workspace_id=ws_id, actor_user_id=identity, pubkey=pubkey)
    await svc.approve_pubkey(workspace_id=ws_id, actor_user_id="owner-1", pubkey_id=pk.id)
    return pk


async def test_register_approve_put_get_happy_path(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    assert pk.status == PUBKEY_STATUS_ACTIVE
    assert pk.attested_at is not None

    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )

    got = await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="cloudflare/api-token")
    # Server round-trips opaque ciphertext byte-for-byte; never decrypts.
    assert got["ciphertext"] == CIPHERTEXT
    assert got["version_number"] == 1
    assert got["alg"] == "age"


async def test_get_default_deny_without_grant(db_session, ws):
    svc = SecretStoreService(db_session)
    pk_alice = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="resend/key",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk_alice.id],
    )
    # Bob has no grant → uniform deny.
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="bob", name="resend/key")

    # The denied attempt is durably logged (fail-closed audit, committed).
    rows = await db_session.execute(
        select(SecretAccessLog).where(
            SecretAccessLog.workspace_id == ws,
            SecretAccessLog.action == "get",
            SecretAccessLog.result == "denied",
        )
    )
    assert len(list(rows.scalars().all())) == 1


async def test_get_unknown_secret_is_uniform_deny(db_session, ws):
    svc = SecretStoreService(db_session)
    # No such secret → same SecretAccessDenied, not a 404 (no existence leak).
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="does/not-exist")


async def test_put_rejects_grant_to_unapproved_pubkey(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await svc.register_pubkey(workspace_id=ws, actor_user_id="alice", pubkey=PUBKEY_A)
    # pk is pending (not approved) → grant rejected.
    with pytest.raises(ValueError, match="approved"):
        await svc.put_secret(
            workspace_id=ws,
            actor_user_id="owner-1",
            name="gcp/sa",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        )


async def test_put_rejects_recipients_snapshot_mismatch(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    # Snapshot claims a DIFFERENT recipient than the grant → integrity error.
    with pytest.raises(ValueError, match="recipients_snapshot"):
        await svc.put_secret(
            workspace_id=ws,
            actor_user_id="owner-1",
            name="gcp/sa",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_B)],
            grant_pubkey_ids=[pk.id],
        )


async def test_put_rejects_invalid_pubkey(db_session, ws):
    svc = SecretStoreService(db_session)
    with pytest.raises(ValueError, match="age recipient"):
        await svc.register_pubkey(workspace_id=ws, actor_user_id="alice", pubkey="not-an-age-key")


async def test_revoke_grant_sets_rotation_and_blocks_future_get(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    # Works before revoke.
    await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="cloudflare/api-token")

    secret = await svc.revoke_grant(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        recipient_pubkey_id=pk.id,
    )
    assert secret.rotation_needed is True

    # Future fetch denied (revoke stops future reads).
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="cloudflare/api-token")


async def test_revoke_pubkey_revokes_dependent_grants(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    await svc.revoke_pubkey(workspace_id=ws, actor_user_id="owner-1", pubkey_id=pk.id)

    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="cloudflare/api-token")


async def test_delete_secret_cascades_and_audit_survives(db_session, ws):
    """Hard-delete (#1153): FK cascade removes versions/grants; audit row survives.

    Exercises the real Postgres ``ondelete=CASCADE`` (part of create_all metadata,
    unlike the trigger) and the FK-decoupling of the audit log: the ``delete``
    entry — and the whole chain — outlive the secret and still verify.
    """
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    secret_id = (
        await db_session.execute(
            select(Secret.id).where(
                Secret.workspace_id == ws, Secret.name == "cloudflare/api-token"
            )
        )
    ).scalar_one()

    await svc.delete_secret(workspace_id=ws, actor_user_id="owner-1", name="cloudflare/api-token")

    # The secret + its versions/grants are gone (DB-level FK cascade).
    assert (
        await db_session.execute(select(Secret).where(Secret.id == secret_id))
    ).scalar_one_or_none() is None
    versions = (
        (
            await db_session.execute(
                select(SecretVersion).where(SecretVersion.secret_id == secret_id)
            )
        )
        .scalars()
        .all()
    )
    grants = (
        (await db_session.execute(select(SecretGrant).where(SecretGrant.secret_id == secret_id)))
        .scalars()
        .all()
    )
    assert list(versions) == []
    assert list(grants) == []

    # The append-only audit trail survives (FK-decoupled) and still verifies; the
    # delete is recorded with the (now-dangling) secret_id for forensics.
    delete_rows = (
        (
            await db_session.execute(
                select(SecretAccessLog).where(
                    SecretAccessLog.workspace_id == ws,
                    SecretAccessLog.action == "delete",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(list(delete_rows)) == 1
    assert delete_rows[0].secret_id == secret_id
    assert (await svc.verify_audit_chain(workspace_id=ws))["valid"] is True

    # The name is free again: a fresh secret can take it (no leftover unique row).
    pk2 = await _register_and_approve(svc, ws, "carol", PUBKEY_B)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_B)],
        grant_pubkey_ids=[pk2.id],
    )
    assert (await svc.list_secrets(workspace_id=ws))[0]["current_version"] == 1


async def test_delete_nonexistent_secret_raises_not_found(db_session, ws):
    """Deleting a name that doesn't exist raises SecretNotFound (→ 404 at the route)."""
    svc = SecretStoreService(db_session)
    with pytest.raises(SecretNotFound):
        await svc.delete_secret(workspace_id=ws, actor_user_id="owner-1", name="nope/missing")


async def test_version_pinning(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    fp = fingerprint_pubkey(PUBKEY_A)
    v1_ct = CIPHERTEXT + "_v1"
    v2_ct = CIPHERTEXT + "_v2"
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=v1_ct,
        recipients_snapshot=[fp],
        grant_pubkey_ids=[pk.id],
    )
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=v2_ct,
        recipients_snapshot=[fp],
        grant_pubkey_ids=[pk.id],
    )
    latest = await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="db/pw")
    assert latest["version_number"] == 2
    assert latest["ciphertext"] == v2_ct

    pinned = await svc.get_secret(
        workspace_id=ws, actor_user_id="alice", name="db/pw", version_number=1
    )
    assert pinned["ciphertext"] == v1_ct


async def test_audit_hash_chain_links(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="db/pw")

    rows = await db_session.execute(
        select(SecretAccessLog)
        .where(SecretAccessLog.workspace_id == ws)
        .order_by(SecretAccessLog.id.asc())
    )
    entries = list(rows.scalars().all())
    assert len(entries) >= 4  # register, approve, put, get
    # First entry chains from genesis; each subsequent prev_hash == prior entry_hash.
    assert entries[0].prev_hash == "0" * 64
    for prev, cur in zip(entries, entries[1:], strict=False):
        assert cur.prev_hash == prev.entry_hash


async def test_list_secrets_never_returns_ciphertext(db_session, ws):
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    listing = await svc.list_secrets(workspace_id=ws)
    assert len(listing) == 1
    entry = listing[0]
    assert entry["name"] == "db/pw"
    assert entry["current_version"] == 1
    assert entry["grant_count"] == 1
    # No ciphertext / value anywhere in the listing payload.
    assert "ciphertext" not in entry
    assert CIPHERTEXT not in str(entry)


async def test_audit_log_append_only_triggers(db_session, ws):
    """The migration's triggers block UPDATE, DELETE, AND TRUNCATE.

    create_all does not install triggers, so we install the migration's DDL here,
    then assert in-band mutation (UPDATE/DELETE) and a full-table TRUNCATE — which
    would silently reset the hash chain to genesis — are all rejected.
    """
    # Install the same triggers the migrations ship (e50 triggers + the
    # e72 carve-out body: UPDATE limited to the erasure identity columns).
    await db_session.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION secret_access_log_no_mutate()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF (to_jsonb(OLD) - ARRAY['actor_user_id', 'recipient_identity'])
                       IS DISTINCT FROM
                       (to_jsonb(NEW) - ARRAY['actor_user_id', 'recipient_identity']) THEN
                        RAISE EXCEPTION
                            'secret_access_log is append-only; only '
                            '(actor_user_id, recipient_identity) may be '
                            'updated (erasure carve-out)'
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'secret_access_log is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'restrict_violation';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log")
    )
    await db_session.execute(
        text(
            """
            CREATE TRIGGER secret_access_log_append_only
            BEFORE UPDATE OR DELETE ON secret_access_log
            FOR EACH ROW EXECUTE FUNCTION secret_access_log_no_mutate();
            """
        )
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log")
    )
    await db_session.execute(
        text(
            """
            CREATE TRIGGER secret_access_log_no_truncate
            BEFORE TRUNCATE ON secret_access_log
            FOR EACH STATEMENT EXECUTE FUNCTION secret_access_log_no_mutate();
            """
        )
    )
    await db_session.commit()
    try:
        svc = SecretStoreService(db_session)
        await svc.register_pubkey(workspace_id=ws, actor_user_id="alice", pubkey=PUBKEY_A)
        await db_session.commit()

        with pytest.raises(Exception, match="append-only"):
            await db_session.execute(
                text("UPDATE secret_access_log SET result = 'error' WHERE workspace_id = :w"),
                {"w": str(ws)},
            )
        await db_session.rollback()

        with pytest.raises(Exception, match="append-only"):
            await db_session.execute(
                text("DELETE FROM secret_access_log WHERE workspace_id = :w"), {"w": str(ws)}
            )
        await db_session.rollback()

        with pytest.raises(Exception, match="append-only"):
            await db_session.execute(text("TRUNCATE secret_access_log"))
        await db_session.rollback()

        # #1365: the erasure carve-out — an UPDATE touching ONLY the
        # identity columns is permitted (pseudonymization path); the
        # non-carve-out UPDATE above stays rejected.
        result = await db_session.execute(
            text(
                "UPDATE secret_access_log SET actor_user_id = 'pseudonym-x', "
                "recipient_identity = NULL WHERE workspace_id = :w"
            ),
            {"w": str(ws)},
        )
        assert result.rowcount >= 1
        await db_session.rollback()
    finally:
        # Drop the triggers so the fixture teardown can wipe rows.
        await db_session.execute(
            text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log")
        )
        await db_session.execute(
            text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log")
        )
        await db_session.execute(text("DROP FUNCTION IF EXISTS secret_access_log_no_mutate()"))
        await db_session.commit()


# --- recall / embedding isolation (gate1 #1128, sibling #210) ------------------


def test_secret_models_have_no_embedding_status():
    """Secret tables must lack an ``embedding_status`` column.

    The embedding sweep query is typed to ``Memory`` and keys off
    ``Memory.embedding_status``; a secret table that never has that column can
    never be enqueued for embedding (one half of the isolation guarantee).
    """
    for model in (RecipientPubkey, Secret, SecretVersion, SecretGrant, SecretAccessLog):
        assert "embedding_status" not in model.__table__.columns


def test_secret_tables_are_not_the_memories_table():
    """Recall reads only the ``memories`` table; secret tables are separate."""
    from models.memory import Memory

    assert Memory.__tablename__ == "memories"
    secret_tables = {
        RecipientPubkey.__tablename__,
        Secret.__tablename__,
        SecretVersion.__tablename__,
        SecretGrant.__tablename__,
        SecretAccessLog.__tablename__,
    }
    assert "memories" not in secret_tables


async def test_stored_secret_not_enqueued_for_embedding(db_session, ws):
    """A stored secret never appears among the embedding sweep's candidates."""
    from models.memory import Memory

    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    # The sweep's candidate query (tasks/embedding_tasks.py) is typed to Memory;
    # replicate its shape and assert no secret data is reachable through it.
    candidates = await db_session.execute(
        select(Memory.id).where(Memory.embedding_status == "pending")
    )
    ids = list(candidates.scalars().all())
    # No Memory rows exist for this workspace's secret — the secret is invisible
    # to the embedding pipeline (different table, no embedding_status column).
    assert all(isinstance(i, uuid.UUID) for i in ids)  # type sanity; never a secret id
    # And the SecretVersion ciphertext is not stored in any Memory row.
    mem_rows = await db_session.execute(select(Memory).where(Memory.summary == CIPHERTEXT))
    assert mem_rows.scalar_one_or_none() is None


# --- all-plans availability (no SKU gate) --------------------------------------


async def test_full_flow_works_on_free_plan(db_session):
    """The secret store has NO plan/SKU gate — the full flow works on 'free'.

    Gating is by workspace role only; nothing in the service or routes branches
    on plan_name. A free-tier workspace (even with the smallest API limit)
    completes register → approve → put → get. (The MCP rate-limit exemption that
    keeps it usable under a low daily quota is pinned in test_secrets_tools.py.)
    """
    ws_id = uuid.uuid4()
    db_session.add(
        Workspace(
            id=ws_id,
            name=f"free-ws-{ws_id}",
            owner_user_id="owner-1",
            plan_name="free",
            daily_api_limit=1,
        )
    )
    await db_session.flush()
    try:
        svc = SecretStoreService(db_session)
        pk = await _register_and_approve(svc, ws_id, "alice", PUBKEY_A)
        await svc.put_secret(
            workspace_id=ws_id,
            actor_user_id="owner-1",
            name="cloudflare/api-token",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
            grant_pubkey_ids=[pk.id],
        )
        got = await svc.get_secret(
            workspace_id=ws_id, actor_user_id="alice", name="cloudflare/api-token"
        )
        assert got["ciphertext"] == CIPHERTEXT
    finally:
        await db_session.rollback()
        await _wipe_audit(db_session)
        await db_session.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": str(ws_id)})
        await db_session.commit()


# --- audit chain verification & integrity (review W2/W4/W5/W7/W8) --------------


async def test_verify_audit_chain_valid_then_detects_tampering(db_session, ws):
    """verify_audit_chain recomputes the HMAC chain and catches a stale entry_hash.

    The 'valid is True' assertion also proves created_at round-trips (verify
    recomputes using the stored created_at.isoformat()).
    """
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="db/pw",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="db/pw")

    ok = await svc.verify_audit_chain(workspace_id=ws)
    assert ok["valid"] is True
    assert ok["entries"] >= 4

    # Tamper a chained field out-of-band (drop the append-only triggers first so
    # the UPDATE is possible — simulating an attacker who bypassed them).
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_no_truncate ON secret_access_log")
    )
    await db_session.execute(
        text("DROP TRIGGER IF EXISTS secret_access_log_append_only ON secret_access_log")
    )
    await db_session.execute(
        text(
            "UPDATE secret_access_log SET recipient_identity = 'evil' "
            "WHERE id = (SELECT min(id) FROM secret_access_log WHERE workspace_id = :w)"
        ),
        {"w": str(ws)},
    )
    await db_session.commit()

    bad = await svc.verify_audit_chain(workspace_id=ws)
    assert bad["valid"] is False
    assert bad["reason"] == "entry_hash_mismatch"


async def test_verify_audit_chain_classifies_erasure_pseudonym(db_session, ws):
    """#1365: an erasure-shaped mutation (64-hex pseudonym in a carve-out
    identity column) is reported in ``erasure_pseudonymized`` — NOT a tamper
    alarm — and the walk continues so all other rows still verify."""
    svc = SecretStoreService(db_session)
    await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await db_session.commit()

    ok = await svc.verify_audit_chain(workspace_id=ws)
    assert ok["valid"] is True
    assert ok["entries"] >= 2
    assert ok["erasure_pseudonymized"] == []

    # Simulate the erasure sweep on the FIRST row (identity columns only —
    # exactly what _erase_secret_access_log writes through the e72 carve-out).
    pseudonym = "ab" * 32  # 64 lowercase hex chars
    await db_session.execute(
        text(
            "UPDATE secret_access_log SET actor_user_id = :p "
            "WHERE id = (SELECT min(id) FROM secret_access_log WHERE workspace_id = :w)"
        ),
        {"p": pseudonym, "w": str(ws)},
    )
    await db_session.commit()

    after = await svc.verify_audit_chain(workspace_id=ws)
    assert after["valid"] is True
    assert len(after["erasure_pseudonymized"]) == 1
    # The scarred row did not stop the walk — every entry was visited and
    # the head hash still reflects the full chain.
    assert after["entries"] == ok["entries"]
    assert after["head"] == ok["head"]


async def test_put_multi_recipient_integrity(db_session, ws):
    """Grant ⇄ recipients_snapshot equality holds with >1 recipient (both directions)."""
    svc = SecretStoreService(db_session)
    pk_a = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    pk_b = await _register_and_approve(svc, ws, "bob", PUBKEY_B)
    fp_a, fp_b = fingerprint_pubkey(PUBKEY_A), fingerprint_pubkey(PUBKEY_B)

    # Both granted + both in snapshot → ok, two active grants, both can fetch.
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="multi",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fp_a, fp_b],
        grant_pubkey_ids=[pk_a.id, pk_b.id],
    )
    listing = await svc.list_secrets(workspace_id=ws)
    assert listing[0]["grant_count"] == 2
    assert (await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="multi"))[
        "ciphertext"
    ] == CIPHERTEXT
    assert (await svc.get_secret(workspace_id=ws, actor_user_id="bob", name="multi"))[
        "ciphertext"
    ] == CIPHERTEXT

    # Subset: grants=[A,B] but snapshot=[A] → reject (a grantee who can't decrypt).
    with pytest.raises(ValueError, match="recipients_snapshot"):
        await svc.put_secret(
            workspace_id=ws,
            actor_user_id="owner-1",
            name="sub",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fp_a],
            grant_pubkey_ids=[pk_a.id, pk_b.id],
        )
    # Superset: grants=[A] but snapshot=[A,B] → reject (decrypts without a grant).
    with pytest.raises(ValueError, match="recipients_snapshot"):
        await svc.put_secret(
            workspace_id=ws,
            actor_user_id="owner-1",
            name="sup",
            ciphertext=CIPHERTEXT,
            recipients_snapshot=[fp_a, fp_b],
            grant_pubkey_ids=[pk_a.id],
        )


async def test_default_deny_wrong_recipient_and_inactive_pubkey(db_session, ws):
    """Default-deny isolates the identity_id and pubkey-status predicates."""
    svc = SecretStoreService(db_session)
    pk_a = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await _register_and_approve(svc, ws, "bob", PUBKEY_B)  # bob approved but NOT granted
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="s",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk_a.id],
    )
    # bob has an active pubkey but no grant → denied (isolates identity_id match).
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="bob", name="s")
    # alice works while her pubkey is active.
    await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="s")

    # Flip alice's pubkey to pending WITHOUT touching the grant → denied (isolates
    # the status == ACTIVE predicate; recipient_pubkeys has no append-only trigger).
    pk_a.status = PUBKEY_STATUS_PENDING
    await db_session.flush()
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="s")


async def test_audit_chain_unique_constraint_prevents_fork(db_session, ws):
    """uq_secret_access_log_ws_prev rejects a second writer on the same chain tip."""
    db_session.add_all(
        [
            SecretAccessLog(
                workspace_id=ws,
                actor_user_id="a",
                action="register",
                result="ok",
                prev_hash="0" * 64,
                entry_hash="a" * 64,
            ),
            SecretAccessLog(
                workspace_id=ws,
                actor_user_id="b",
                action="register",
                result="ok",
                prev_hash="0" * 64,
                entry_hash="b" * 64,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_get_missing_version_logs_denied_audit(db_session, ws):
    """A grant-OK but missing-version get raises SecretNotFound AND commits a denied audit."""
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="s",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    with pytest.raises(SecretNotFound):
        await svc.get_secret(workspace_id=ws, actor_user_id="alice", name="s", version_number=2)

    rows = await db_session.execute(
        select(SecretAccessLog).where(
            SecretAccessLog.workspace_id == ws,
            SecretAccessLog.action == "get",
            SecretAccessLog.result == "denied",
        )
    )
    denied = list(rows.scalars().all())
    assert len(denied) == 1
    assert denied[0].secret_id is not None  # the secret was found; only the version was missing


async def test_denied_get_audits_secret_id_when_secret_exists(db_session, ws):
    """An unauthorized probe of a REAL secret name records secret_id server-side.

    The caller still gets a uniform SecretAccessDenied (no existence leak), but
    the audit attributes the probe to the secret for incident response.
    """
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="cloudflare/api-token",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
    )
    # bob has no grant; probing the REAL name is denied but logged with secret_id.
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="bob", name="cloudflare/api-token")
    # probing a NON-existent name is denied with secret_id NULL.
    with pytest.raises(SecretAccessDenied):
        await svc.get_secret(workspace_id=ws, actor_user_id="bob", name="does/not-exist")

    rows = await db_session.execute(
        select(SecretAccessLog)
        .where(SecretAccessLog.workspace_id == ws, SecretAccessLog.result == "denied")
        .order_by(SecretAccessLog.id.asc())
    )
    denied = list(rows.scalars().all())
    assert len(denied) == 2
    assert denied[0].secret_id is not None  # real-secret probe → attributable
    assert denied[1].secret_id is None  # nonexistent-name probe → no id


async def test_req_meta_persisted_and_chain_still_verifies(db_session, ws):
    """req_meta (source/ip/ua) is stored and does not break the HMAC chain."""
    svc = SecretStoreService(db_session)
    pk = await _register_and_approve(svc, ws, "alice", PUBKEY_A)
    await svc.put_secret(
        workspace_id=ws,
        actor_user_id="owner-1",
        name="s",
        ciphertext=CIPHERTEXT,
        recipients_snapshot=[fingerprint_pubkey(PUBKEY_A)],
        grant_pubkey_ids=[pk.id],
        req_meta={"source": "rest"},
    )
    await svc.get_secret(
        workspace_id=ws,
        actor_user_id="alice",
        name="s",
        req_meta={"source": "mcp", "tool": "secret_get"},
    )
    # The get audit row carries the metadata.
    get_rows = await db_session.execute(
        select(SecretAccessLog).where(
            SecretAccessLog.workspace_id == ws, SecretAccessLog.action == "get"
        )
    )
    get_entry = get_rows.scalar_one()
    assert get_entry.req_meta == {"source": "mcp", "tool": "secret_get"}
    # Chain still verifies with req_meta populated (payload includes it).
    assert (await svc.verify_audit_chain(workspace_id=ws))["valid"] is True
