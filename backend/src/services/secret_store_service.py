"""Zero-knowledge secret store service (Issue #1128).

Server-side logic for a passive, ciphertext-only secret store. The server
**never decrypts**: it validates the *shape* of an ``age`` recipient key and
stores/returns opaque ciphertext, but performs no cryptography on secret values
and holds no key that could decrypt them. It deliberately does NOT use
``utils.encryption`` (Fernet, server-custody) — that path is for credentials the
server must *use*; these the server only *persists*.

Security invariants enforced here (gate1 #1128):
- **Default-deny** reads: ``get_secret`` returns ciphertext only when an active
  grant links the secret to one of the caller's *active* (owner-approved)
  recipient pubkeys.
- **Fail-closed audit**: every read writes an audit entry and **commits it
  before** the ciphertext is returned; a denied or errored read is logged too.
- **Grant ⇄ ciphertext integrity**: ``put_secret`` rejects a version whose
  ``recipients_snapshot`` does not exactly match the granted active pubkeys.
- **Tamper-evident audit**: ``secret_access_log`` is a per-workspace HMAC hash
  chain; appends are serialized with a per-workspace advisory lock so the chain
  stays strictly linear. DB-level append-only is enforced by a trigger (migration).
- **No plaintext in logs**: secret values are never written to the audit log,
  application logs, or exception text.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.secrets import (
    AUDIT_ACTION_APPROVE,
    AUDIT_ACTION_GET,
    AUDIT_ACTION_PUT,
    AUDIT_ACTION_REGISTER,
    AUDIT_ACTION_REVOKE,
    AUDIT_GENESIS_HASH,
    AUDIT_RESULT_DENIED,
    AUDIT_RESULT_OK,
    PUBKEY_STATUS_ACTIVE,
    PUBKEY_STATUS_PENDING,
    PUBKEY_STATUS_REVOKED,
    SECRET_ALG_AGE,
    SECRET_STATUS_ACTIVE,
    RecipientPubkey,
    Secret,
    SecretAccessLog,
    SecretGrant,
    SecretVersion,
)
from utils.datetime import utcnow
from utils.hashing import hmac_sha256_hex, sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# Shape check for an ``age`` X25519 recipient (``age1`` + bech32 lowercase body).
# This is a sanity gate, not cryptographic validation — the SDK / age library is
# authoritative. We only reject obviously-wrong input so the store stays clean.
_AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{20,110}$")

# Inline ciphertext cap. An ``age``-wrapped API key is small (≲ a few KB); a
# generous ceiling bounds request bodies without forcing R2 for normal use.
CIPHERTEXT_MAX_LEN = 256 * 1024


class SecretStoreError(Exception):
    """Base error for the secret store."""


class SecretNotFound(SecretStoreError):
    """A referenced pubkey / secret / grant does not exist (owner-facing 404)."""


class SecretAccessDenied(SecretStoreError):
    """A read was denied by the default-deny grant check (uniform 403).

    Raised identically for "no such secret" and "no grant" so a caller cannot
    probe which secrets exist in a workspace they lack access to.
    """


def fingerprint_pubkey(pubkey: str) -> str:
    """Stable SHA256 fingerprint of an ``age`` recipient pubkey."""
    return sha256_hex(pubkey)


class SecretStoreService:
    """Async service over the ``secret_*`` / ``recipient_pubkeys`` tables.

    Commit policy: management writes (register / approve / revoke / put) only
    ``flush()`` — the caller (route) commits — mirroring the repo convention.
    ``get_secret`` is the deliberate exception: it **commits its own audit
    entry** before returning ciphertext, so a read is never served without a
    durably-logged access (fail-closed).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------ audit
    async def _append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: str,
        action: str,
        result: str,
        secret_id: UUID | None = None,
        version_id: UUID | None = None,
        recipient_identity: str | None = None,
        req_meta: dict | None = None,
    ) -> SecretAccessLog:
        """Append one tamper-evident entry to the workspace's audit chain.

        Takes a per-workspace transaction-scoped advisory lock so concurrent
        appends serialize and the hash chain stays strictly linear (the
        ``(workspace_id, prev_hash)`` unique constraint is the backstop). Never
        receives or stores a secret value.
        """
        # Serialize appends for this workspace within the transaction. Use the
        # 64-bit hashtextextended (PR #686 standard — hashtext's 32-bit space
        # birthday-collides across workspaces) with a feature-namespaced key so
        # secret-audit locks never share a value with quota/connector locks.
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"secret_audit:{workspace_id}"},
        )

        prev_row = await self.db.execute(
            select(SecretAccessLog.entry_hash)
            .where(SecretAccessLog.workspace_id == workspace_id)
            .order_by(SecretAccessLog.id.desc())
            .limit(1)
        )
        prev_hash = prev_row.scalar_one_or_none() or AUDIT_GENESIS_HASH

        created_at = utcnow()
        payload = self._audit_payload(
            prev_hash=prev_hash,
            workspace_id=workspace_id,
            secret_id=secret_id,
            version_id=version_id,
            recipient_identity=recipient_identity,
            actor_user_id=actor_user_id,
            action=action,
            result=result,
            created_at=created_at,
            req_meta=req_meta,
        )
        entry_hash = hmac_sha256_hex(payload, get_settings().audit_hmac_key)

        entry = SecretAccessLog(
            workspace_id=workspace_id,
            secret_id=secret_id,
            version_id=version_id,
            recipient_identity=recipient_identity,
            actor_user_id=actor_user_id,
            action=action,
            result=result,
            req_meta=req_meta,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            created_at=created_at,
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    @staticmethod
    def _audit_payload(
        *,
        prev_hash: str,
        workspace_id: UUID,
        secret_id: UUID | None,
        version_id: UUID | None,
        recipient_identity: str | None,
        actor_user_id: str,
        action: str,
        result: str,
        created_at: Any,
        req_meta: dict | None,
    ) -> str:
        """Canonical, length-safe HMAC input for one audit entry.

        JSON-encodes the chained fields (rather than a ``|``-join) so a value
        containing a delimiter cannot shift field boundaries once ``req_meta``
        carries free-form tool/source metadata. Used identically by append and
        by :meth:`verify_audit_chain`, so the chain is recomputable.
        """
        return json.dumps(
            [
                prev_hash,
                str(workspace_id),
                str(secret_id or ""),
                str(version_id or ""),
                recipient_identity or "",
                actor_user_id,
                action,
                result,
                created_at.isoformat(),
                req_meta or {},
            ],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    async def verify_audit_chain(self, *, workspace_id: UUID) -> dict[str, Any]:
        """Recompute and verify a workspace's tamper-evident audit chain.

        Walks entries id-ascending, recomputing ``entry_hash`` from the stored
        fields with the same payload construction as the append path, and checks
        the ``prev_hash`` linkage (genesis for the first). Returns
        ``{valid, entries, head}`` on success, or ``{valid: False, broken_at,
        reason}`` at the first entry that fails — catching any out-of-band field
        edit that left ``entry_hash`` stale. This is the verifier that makes the
        chain's tamper-evidence usable (not merely latent).
        """
        rows = await self.db.execute(
            select(SecretAccessLog)
            .where(SecretAccessLog.workspace_id == workspace_id)
            .order_by(SecretAccessLog.id.asc())
        )
        entries = list(rows.scalars().all())
        key = get_settings().audit_hmac_key
        expected_prev = AUDIT_GENESIS_HASH
        for e in entries:
            if e.prev_hash != expected_prev:
                return {"valid": False, "broken_at": e.id, "reason": "prev_hash_mismatch"}
            payload = self._audit_payload(
                prev_hash=e.prev_hash,
                workspace_id=e.workspace_id,
                secret_id=e.secret_id,
                version_id=e.version_id,
                recipient_identity=e.recipient_identity,
                actor_user_id=e.actor_user_id,
                action=e.action,
                result=e.result,
                created_at=e.created_at,
                req_meta=e.req_meta,
            )
            if hmac_sha256_hex(payload, key) != e.entry_hash:
                return {"valid": False, "broken_at": e.id, "reason": "entry_hash_mismatch"}
            expected_prev = e.entry_hash
        return {
            "valid": True,
            "entries": len(entries),
            "head": expected_prev if entries else AUDIT_GENESIS_HASH,
        }

    # -------------------------------------------------------------- pubkeys
    @staticmethod
    def _validate_pubkey(pubkey: str) -> None:
        if not _AGE_RECIPIENT_RE.match(pubkey or ""):
            raise ValueError(
                "pubkey must be an age recipient key (age1… bech32). "
                "Generate one with `age-keygen` and register only the public recipient."
            )

    async def register_pubkey(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: str,
        pubkey: str,
        label: str | None = None,
    ) -> RecipientPubkey:
        """Register the caller's own ``age`` recipient pubkey as ``pending``.

        ``identity_id`` is bound to the authenticated caller — a member cannot
        register a key claiming to be someone else. An owner must ``approve`` it
        (TOFU attestation) before it can be a grant target.
        """
        self._validate_pubkey(pubkey)
        fp = fingerprint_pubkey(pubkey)

        existing = await self.db.execute(
            select(RecipientPubkey).where(
                RecipientPubkey.workspace_id == workspace_id,
                RecipientPubkey.pubkey == pubkey,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError("This recipient pubkey is already registered in the workspace")

        row = RecipientPubkey(
            workspace_id=workspace_id,
            identity_id=actor_user_id,
            pubkey=pubkey,
            fingerprint=fp,
            label=label,
            status=PUBKEY_STATUS_PENDING,
            created_by=actor_user_id,
        )
        self.db.add(row)
        await self.db.flush()

        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_REGISTER,
            result=AUDIT_RESULT_OK,
            recipient_identity=actor_user_id,
        )
        logger.info(
            "secret_pubkey_registered",
            workspace_id=str(workspace_id),
            identity_id=actor_user_id,
            fingerprint=fp[:12],
        )
        return row

    async def _load_pubkey(self, workspace_id: UUID, pubkey_id: UUID) -> RecipientPubkey:
        result = await self.db.execute(
            select(RecipientPubkey).where(
                RecipientPubkey.id == pubkey_id,
                RecipientPubkey.workspace_id == workspace_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise SecretNotFound("Recipient pubkey not found")
        return row

    async def approve_pubkey(
        self, *, workspace_id: UUID, actor_user_id: str, pubkey_id: UUID
    ) -> RecipientPubkey:
        """Owner-approve a pending pubkey → ``active`` (the TOFU trust gate)."""
        row = await self._load_pubkey(workspace_id, pubkey_id)
        if row.status != PUBKEY_STATUS_PENDING:
            raise ValueError(f"Pubkey is not pending (status={row.status})")
        row.status = PUBKEY_STATUS_ACTIVE
        row.attested_at = utcnow()
        row.attested_by = actor_user_id
        await self.db.flush()

        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_APPROVE,
            result=AUDIT_RESULT_OK,
            recipient_identity=row.identity_id,
        )
        logger.info(
            "secret_pubkey_approved",
            workspace_id=str(workspace_id),
            identity_id=row.identity_id,
            approver=actor_user_id,
        )
        return row

    async def revoke_pubkey(
        self, *, workspace_id: UUID, actor_user_id: str, pubkey_id: UUID
    ) -> RecipientPubkey:
        """Owner-revoke a pubkey and revoke every grant that targets it.

        Affected secrets are flagged ``rotation_needed`` — the holder may have
        already fetched ciphertext, so the upstream credential must be rotated
        (revocation is not retroactive un-sharing).
        """
        row = await self._load_pubkey(workspace_id, pubkey_id)
        if row.status == PUBKEY_STATUS_REVOKED:
            raise ValueError("Pubkey is already revoked")
        now = utcnow()
        row.status = PUBKEY_STATUS_REVOKED
        row.revoked_at = now

        grants = await self.db.execute(
            select(SecretGrant).where(
                SecretGrant.recipient_pubkey_id == pubkey_id,
                SecretGrant.revoked_at.is_(None),
            )
        )
        affected = list(grants.scalars().all())
        secret_ids = {g.secret_id for g in affected}
        for grant in affected:
            grant.revoked_at = now
        if secret_ids:
            # One bulk UPDATE to flag rotation on every affected secret, instead
            # of a per-grant SELECT (a shared key may be granted on many secrets).
            await self.db.execute(
                update(Secret).where(Secret.id.in_(secret_ids)).values(rotation_needed=True)
            )
        await self.db.flush()

        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_REVOKE,
            result=AUDIT_RESULT_OK,
            recipient_identity=row.identity_id,
        )
        logger.info(
            "secret_pubkey_revoked",
            workspace_id=str(workspace_id),
            identity_id=row.identity_id,
            revoker=actor_user_id,
        )
        return row

    async def list_pubkeys(
        self, *, workspace_id: UUID, identity_id: str | None = None
    ) -> list[RecipientPubkey]:
        """List recipient pubkeys in a workspace (optionally one identity's)."""
        stmt = select(RecipientPubkey).where(RecipientPubkey.workspace_id == workspace_id)
        if identity_id is not None:
            stmt = stmt.where(RecipientPubkey.identity_id == identity_id)
        stmt = stmt.order_by(RecipientPubkey.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # --------------------------------------------------------------- secrets
    async def _active_pubkeys_by_ids(
        self, workspace_id: UUID, pubkey_ids: list[UUID]
    ) -> list[RecipientPubkey]:
        if not pubkey_ids:
            return []
        result = await self.db.execute(
            select(RecipientPubkey).where(
                RecipientPubkey.workspace_id == workspace_id,
                RecipientPubkey.id.in_(pubkey_ids),
            )
        )
        return list(result.scalars().all())

    async def put_secret(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: str,
        name: str,
        ciphertext: str,
        recipients_snapshot: list[str],
        grant_pubkey_ids: list[UUID],
        req_meta: dict | None = None,
    ) -> tuple[Secret, SecretVersion]:
        """Store a new ciphertext version of ``name`` and reconcile its grants.

        The server never sees plaintext. ``recipients_snapshot`` (the
        fingerprints the ciphertext was encrypted to) must equal exactly the
        fingerprints of ``grant_pubkey_ids`` — all of which must be *active*
        (owner-approved) — or the put is rejected (grant ⇄ ciphertext integrity).
        After the put, the secret's active grants are exactly ``grant_pubkey_ids``.
        """
        if not name or len(name) > 255:
            raise ValueError("Secret name must be 1–255 characters")
        if not ciphertext:
            raise ValueError("ciphertext is required (the server never receives plaintext)")
        if len(ciphertext) > CIPHERTEXT_MAX_LEN:
            raise ValueError(f"ciphertext exceeds {CIPHERTEXT_MAX_LEN} bytes")
        if not grant_pubkey_ids:
            raise ValueError("At least one grant recipient is required")

        # All grant targets must exist in this workspace and be active (approved).
        pubkeys = await self._active_pubkeys_by_ids(workspace_id, grant_pubkey_ids)
        found_ids = {p.id for p in pubkeys}
        missing = [str(pid) for pid in grant_pubkey_ids if pid not in found_ids]
        if missing:
            raise ValueError(f"Unknown recipient pubkey(s) in this workspace: {missing}")
        not_active = [p.fingerprint[:12] for p in pubkeys if p.status != PUBKEY_STATUS_ACTIVE]
        if not_active:
            raise ValueError(
                f"Grants may only target approved (active) pubkeys; not approved: {not_active}"
            )

        # Grant ⇄ ciphertext integrity: the encrypted-to set must equal the
        # granted set exactly (no recipient who can decrypt but lacks a grant,
        # and no grant whose recipient cannot decrypt this ciphertext).
        granted_fps = {p.fingerprint for p in pubkeys}
        if len(recipients_snapshot) != len(set(recipients_snapshot)):
            raise ValueError("recipients_snapshot contains duplicate fingerprints")
        snapshot_fps = set(recipients_snapshot or [])
        if snapshot_fps != granted_fps:
            raise ValueError(
                "recipients_snapshot does not match the granted recipients "
                "(the ciphertext must be encrypted to exactly the granted pubkeys)"
            )

        # Serialize concurrent puts to the SAME secret so the get-or-create +
        # version-number computation cannot race — otherwise two puts of the same
        # name compute the same version_number / both insert the secret, and the
        # loser surfaces a raw unique-constraint IntegrityError (an opaque 5xx).
        # Released at commit. (The audit advisory lock is keyed differently, so
        # these don't contend with audit appends.)
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"secret_put:{workspace_id}:{name}"},
        )

        # Get-or-create the secret.
        existing = await self.db.execute(
            select(Secret).where(Secret.workspace_id == workspace_id, Secret.name == name)
        )
        secret = existing.scalar_one_or_none()
        if secret is None:
            secret = Secret(
                workspace_id=workspace_id,
                name=name,
                status=SECRET_STATUS_ACTIVE,
                created_by=actor_user_id,
            )
            self.db.add(secret)
            await self.db.flush()
        else:
            secret.status = SECRET_STATUS_ACTIVE
            # A fresh version supersedes the rotation request.
            secret.rotation_needed = False

        # Next monotonic version number.
        max_ver = await self.db.execute(
            select(SecretVersion.version_number)
            .where(SecretVersion.secret_id == secret.id)
            .order_by(SecretVersion.version_number.desc())
            .limit(1)
        )
        next_version = (max_ver.scalar_one_or_none() or 0) + 1

        version = SecretVersion(
            secret_id=secret.id,
            version_number=next_version,
            ciphertext=ciphertext,
            alg=SECRET_ALG_AGE,
            recipients_snapshot=sorted(snapshot_fps),
            created_by=actor_user_id,
        )
        self.db.add(version)
        await self.db.flush()

        await self._reconcile_grants(secret.id, grant_pubkey_ids, actor_user_id)

        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_PUT,
            result=AUDIT_RESULT_OK,
            secret_id=secret.id,
            version_id=version.id,
            req_meta=req_meta,
        )
        logger.info(
            "secret_put",
            workspace_id=str(workspace_id),
            name=name,
            version=next_version,
            recipients=len(granted_fps),
        )
        return secret, version

    async def _reconcile_grants(
        self, secret_id: UUID, grant_pubkey_ids: list[UUID], actor_user_id: str
    ) -> None:
        """Make the secret's active grants exactly ``grant_pubkey_ids``."""
        target = set(grant_pubkey_ids)
        now = utcnow()
        result = await self.db.execute(
            select(SecretGrant).where(SecretGrant.secret_id == secret_id)
        )
        existing = {g.recipient_pubkey_id: g for g in result.scalars().all()}

        for pid in target:
            grant = existing.get(pid)
            if grant is None:
                self.db.add(
                    SecretGrant(
                        secret_id=secret_id,
                        recipient_pubkey_id=pid,
                        granted_by=actor_user_id,
                    )
                )
            elif grant.revoked_at is not None:
                grant.revoked_at = None  # re-activate
                grant.granted_by = actor_user_id
                grant.granted_at = now

        for pid, grant in existing.items():
            if pid not in target and grant.revoked_at is None:
                grant.revoked_at = now
        await self.db.flush()

    async def get_secret(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: str,
        name: str,
        version_number: int | None = None,
        req_meta: dict | None = None,
    ) -> dict[str, Any]:
        """Return the requested ciphertext version — default-deny, audit-first.

        Allowed only when an active grant links the secret to one of the
        caller's *active* recipient pubkeys. The access (ok **or** denied) is
        written to the audit chain and **committed before** any ciphertext is
        returned; a denied read raises :class:`SecretAccessDenied` after logging.
        The server returns opaque ciphertext and never decrypts it.
        """
        secret = await self._load_active_secret(workspace_id, name)
        authorized = secret is not None and await self._caller_has_active_grant(
            secret.id, actor_user_id, workspace_id
        )
        if not authorized:
            # Uniform denial to the CALLER (raise the same error whether the
            # secret is missing or just ungranted) — but record secret_id in the
            # server-side audit when the secret DOES exist, so an unauthorized
            # probe of a real secret name is attributable for incident response
            # and rotation. The audit is never returned to the caller, so this
            # records nothing the caller doesn't already supply (the name).
            await self._append_audit(
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                action=AUDIT_ACTION_GET,
                result=AUDIT_RESULT_DENIED,
                secret_id=(secret.id if secret is not None else None),
                recipient_identity=actor_user_id,
                req_meta=req_meta,
            )
            await self.db.commit()
            logger.warning(
                "secret_get_denied",
                workspace_id=str(workspace_id),
                actor=actor_user_id,
                name=name,
                secret_exists=secret is not None,
            )
            raise SecretAccessDenied("Access denied")

        version = await self._select_version(secret.id, version_number)
        if version is None:
            await self._append_audit(
                workspace_id=workspace_id,
                actor_user_id=actor_user_id,
                action=AUDIT_ACTION_GET,
                result=AUDIT_RESULT_DENIED,
                secret_id=secret.id,
                recipient_identity=actor_user_id,
                req_meta=req_meta,
            )
            await self.db.commit()
            raise SecretNotFound("Secret version not found")

        # Extract the response BEFORE committing so post-commit expiry can't bite.
        payload: dict[str, Any] = {
            "name": secret.name,
            "version_number": version.version_number,
            "alg": version.alg,
            "ciphertext": version.ciphertext,
            "blob_ref": version.blob_ref,
            "recipients_snapshot": version.recipients_snapshot,
            "rotation_needed": secret.rotation_needed,
            "created_at": version.created_at,
        }

        # Fail-closed: the access is durably logged before the ciphertext leaves.
        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_GET,
            result=AUDIT_RESULT_OK,
            secret_id=secret.id,
            version_id=version.id,
            recipient_identity=actor_user_id,
            req_meta=req_meta,
        )
        await self.db.commit()
        logger.info(
            "secret_get",
            workspace_id=str(workspace_id),
            actor=actor_user_id,
            name=name,
            version=version.version_number,
        )
        return payload

    async def _load_active_secret(self, workspace_id: UUID, name: str) -> Secret | None:
        """Load an active secret by name (existence check; no grant evaluation)."""
        secret_row = await self.db.execute(
            select(Secret).where(
                Secret.workspace_id == workspace_id,
                Secret.name == name,
                Secret.status == SECRET_STATUS_ACTIVE,
            )
        )
        return secret_row.scalar_one_or_none()

    async def _caller_has_active_grant(
        self, secret_id: UUID, actor_user_id: str, workspace_id: UUID
    ) -> bool:
        """True iff an active grant links the secret to one of the caller's active pubkeys."""
        grant_row = await self.db.execute(
            select(SecretGrant.id)
            .join(RecipientPubkey, SecretGrant.recipient_pubkey_id == RecipientPubkey.id)
            .where(
                SecretGrant.secret_id == secret_id,
                SecretGrant.revoked_at.is_(None),
                # Defense-in-depth: the most security-critical query is self-
                # contained — even though grants are validated same-workspace at
                # put time, re-assert the workspace on the recipient here.
                RecipientPubkey.workspace_id == workspace_id,
                RecipientPubkey.identity_id == actor_user_id,
                RecipientPubkey.status == PUBKEY_STATUS_ACTIVE,
            )
            .limit(1)
        )
        return grant_row.scalar_one_or_none() is not None

    async def _select_version(
        self, secret_id: UUID, version_number: int | None
    ) -> SecretVersion | None:
        stmt = select(SecretVersion).where(SecretVersion.secret_id == secret_id)
        if version_number is not None:
            stmt = stmt.where(SecretVersion.version_number == version_number)
        else:
            stmt = stmt.order_by(SecretVersion.version_number.desc())
        stmt = stmt.limit(1)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke_grant(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: str,
        name: str,
        recipient_pubkey_id: UUID,
    ) -> Secret:
        """Revoke one recipient's grant on a secret and flag rotation.

        Revocation stops *future* fetches; it does not un-share ciphertext the
        recipient may already hold, so the secret is flagged ``rotation_needed``.
        """
        secret_row = await self.db.execute(
            select(Secret).where(Secret.workspace_id == workspace_id, Secret.name == name)
        )
        secret = secret_row.scalar_one_or_none()
        if secret is None:
            raise SecretNotFound("Secret not found")

        grant_row = await self.db.execute(
            select(SecretGrant).where(
                SecretGrant.secret_id == secret.id,
                SecretGrant.recipient_pubkey_id == recipient_pubkey_id,
                SecretGrant.revoked_at.is_(None),
            )
        )
        grant = grant_row.scalar_one_or_none()
        if grant is None:
            raise SecretNotFound("Active grant not found")

        grant.revoked_at = utcnow()
        secret.rotation_needed = True
        await self.db.flush()

        await self._append_audit(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION_REVOKE,
            result=AUDIT_RESULT_OK,
            secret_id=secret.id,
        )
        logger.info(
            "secret_grant_revoked",
            workspace_id=str(workspace_id),
            name=name,
            revoker=actor_user_id,
        )
        return secret

    async def list_secrets(self, *, workspace_id: UUID) -> list[dict[str, Any]]:
        """List secret names + metadata. **Never** returns ciphertext/values.

        Current-version and active-grant-count are each fetched in ONE grouped
        query over the whole secret set (not per-secret), so the console/MCP
        listing stays at 3 round-trips regardless of secret count (no N+1).
        """
        result = await self.db.execute(
            select(Secret).where(Secret.workspace_id == workspace_id).order_by(Secret.name.asc())
        )
        secrets = list(result.scalars().all())
        if not secrets:
            return []
        ids = [s.id for s in secrets]

        ver_rows = await self.db.execute(
            select(SecretVersion.secret_id, func.max(SecretVersion.version_number))
            .where(SecretVersion.secret_id.in_(ids))
            .group_by(SecretVersion.secret_id)
        )
        ver_map = dict(ver_rows.all())

        grant_rows = await self.db.execute(
            select(SecretGrant.secret_id, func.count())
            .where(SecretGrant.secret_id.in_(ids), SecretGrant.revoked_at.is_(None))
            .group_by(SecretGrant.secret_id)
        )
        grant_map = dict(grant_rows.all())

        return [
            {
                "name": s.name,
                "status": s.status,
                "rotation_needed": s.rotation_needed,
                "current_version": ver_map.get(s.id),
                "grant_count": grant_map.get(s.id, 0),
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in secrets
        ]
