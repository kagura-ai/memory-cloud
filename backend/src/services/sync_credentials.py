"""Durable service-token issuance and the mandatory paid sync consumer boundary."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import event, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from auth.oidc_config import _issuer
from auth.sync_service_jwt import (
    ASSERTION_TYPE,
    ClientAssertion,
    InvalidAssertion,
    assertion_header,
    key_fingerprint,
    verify_assertion,
)
from config.settings import get_settings
from db.base import isolated_session
from models.auth import AuditLog, Context
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from models.sync_credential import (
    SyncAccessToken,
    SyncAssertionUse,
    SyncCredentialClient,
    SyncSigningKey,
)
from models.sync_installation import SYNC_OPERATIONS, SyncInstallation, SyncService
from services.integration_entitlements import paid_integration_write
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available

TOKEN_PATTERN = re.compile(r"kmc_sync_at_[A-Za-z0-9_-]{43}\Z")


class SyncCredentialError(ValueError):
    """Fixed protocol error with no reflected input, database or JOSE text."""

    def __init__(self, error: str = "invalid_token", status: int = 401):
        super().__init__(error)
        self.error, self.status = error, status


def sync_issuer() -> str:
    """Use only the operator's fixed issuer, never an incoming Host or key URL."""
    settings = get_settings()
    if not (
        settings.oidc_enabled
        and settings.sync_integrations_enabled
        and settings.sync_credentials_enabled
    ):
        raise SyncCredentialError("temporarily_unavailable", 503)
    return _issuer(settings.oidc_issuer)


def resource_uri(issuer: str, installation_id: UUID) -> str:
    """Canonical per-installation resource identifier, distinct from user audiences."""
    return issuer + "/sync/installations/" + str(installation_id)


async def _clean_session(db: AsyncSession) -> None:
    if db.new or db.dirty or db.deleted or db.info.get("paid_integration_write"):
        raise RuntimeError("Sync credentials require a fresh unguarded transaction")
    if await db.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is not None:
        raise RuntimeError("Sync credentials must precede all writes and row locks")


@dataclass(frozen=True)
class ClientProof:
    assertion: ClientAssertion
    key_id: UUID
    fingerprint: str
    service_id: str
    service_generation: int
    client_generation: int


async def _authenticate(db: AsyncSession, form: dict[str, str], issuer: str) -> ClientProof:
    try:
        header = assertion_header(form["client_assertion"])
        row = (
            await db.execute(
                select(SyncCredentialClient, SyncSigningKey, SyncService)
                .join(SyncSigningKey, SyncSigningKey.client_id == SyncCredentialClient.client_id)
                .join(SyncService, SyncService.service_id == SyncCredentialClient.service_id)
                .where(
                    SyncCredentialClient.client_id == form["client_id"],
                    SyncSigningKey.kid == header["kid"],
                )
                .execution_options(populate_existing=True)
            )
        ).first()
        if row is None:
            raise InvalidAssertion()
        client, key, service = row
        if (
            not client.enabled
            or not service.enabled
            or key.revoked_at is not None
            or client.client_id != service.service_client_id
        ):
            raise InvalidAssertion()
        proof = verify_assertion(
            form["client_assertion"],
            key.public_jwk,
            client_id=client.client_id,
            issuer=issuer,
            now=int(utcnow().replace(tzinfo=UTC).timestamp()),
        )
        fingerprint = key_fingerprint(key.public_jwk)
        if fingerprint != key.fingerprint:
            raise InvalidAssertion()
        return ClientProof(
            proof, key.id, fingerprint, service.service_id, service.generation, client.generation
        )
    except (InvalidAssertion, ValueError):
        raise SyncCredentialError("invalid_client", 400) from None


async def _consume_assertion(db: AsyncSession, proof: ClientProof) -> None:
    # The caller releases its plain-read transaction first. This receipt commits
    # independently: later paid/target denial or a lost response never unspends it.
    async with isolated_session(db) as replay:
        result = await replay.execute(
            insert(SyncAssertionUse)
            .values(
                client_id=proof.assertion.client_id,
                jti_hash=proof.assertion.jti_hash,
                expires_at=proof.assertion.expires_at,
            )
            .on_conflict_do_nothing()
            .returning(SyncAssertionUse.jti_hash)
        )
        if result.scalar_one_or_none() is None:
            raise SyncCredentialError("invalid_client", 400)
        await replay.commit()


async def _locked(db: AsyncSession, model: Any, predicate: Any) -> Any:
    try:
        return await db.scalar(
            select(model)
            .where(predicate)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise SyncCredentialError("temporarily_unavailable", 503) from None
        raise


@dataclass
class CredentialAuthority:
    """Rows held through commit; the consumer may write content, never authority."""

    service: SyncService
    client: SyncCredentialClient
    key: SyncSigningKey
    installation: SyncInstallation
    resource: Resource
    context: Context
    endpoint: PublicMcpEndpoint
    issuer: str
    snapshot: tuple = field(repr=False)
    deadline: datetime
    paid_until: datetime
    token: SyncAccessToken | None = field(default=None, repr=False)
    token_snapshot: tuple | None = field(default=None, repr=False)

    def token_identity(self) -> tuple | None:
        """Snapshot token authority, including scope, generation and fixed time bounds."""
        row = self.token
        if row is None:
            return None
        return (
            row.token_hash,
            row.installation_id,
            row.workspace_id,
            row.service_id,
            row.client_id,
            row.key_id,
            row.key_fingerprint,
            row.service_generation,
            row.client_generation,
            row.installation_generation,
            row.resource_pk,
            row.context_id,
            row.endpoint_id,
            row.resource_id,
            tuple(row.operations),
            row.issuer,
            row.resource,
            row.scope,
            row.created_at,
            row.expires_at,
            row.revoked_at,
        )

    def identity(self) -> tuple:
        row = self.installation
        return (
            self.service.service_id,
            self.service.service_client_id,
            self.service.generation,
            self.client.client_id,
            self.client.service_id,
            self.client.generation,
            self.key.id,
            self.key.client_id,
            self.key.fingerprint,
            row.id,
            row.workspace_id,
            row.service_id,
            row.service_client_id,
            row.generation,
            row.resource_pk,
            row.context_id,
            row.endpoint_id,
            row.resource_id,
            tuple(row.operations),
            self.resource.id,
            self.resource.workspace_id,
            self.resource.resource_id,
            self.context.id,
            self.context.workspace_id,
            self.context.resource_id,
            self.endpoint.id,
            self.endpoint.workspace_id,
            self.endpoint.context_id,
            self.endpoint.resource_pk,
            self.endpoint.resource_id,
        )

    def check(self, _session: Session | None = None) -> None:
        """Synchronous final fence after every await, immediately before commit."""
        now = utcnow()
        if (
            sync_issuer() != self.issuer
            or self.identity() != self.snapshot
            or not self.service.enabled
            or not self.client.enabled
            or self.key.revoked_at is not None
            or key_fingerprint(self.key.public_jwk) != self.key.fingerprint
            or self.key.public_jwk.get("kid") != self.key.kid
            or self.installation.state != "active"
            or self.installation.revoked_at is not None
            or self.context.deleted_at is not None
            or not self.context.is_public
            or self.context.is_private
            or self.endpoint.revoked_at is not None
            or now >= min(self.deadline, self.paid_until)
            or (
                self.token is not None
                and (
                    self.token.revoked_at is not None
                    or now >= self.token.expires_at
                    or self.token.created_at > now
                    or self.token.expires_at - self.token.created_at != timedelta(seconds=300)
                    or self.token_identity() != self.token_snapshot
                )
            )
        ):
            raise SyncCredentialError()


@asynccontextmanager
async def _authority(
    db: AsyncSession,
    installation_id: UUID,
    service_id: str,
    client_id: str,
    key_id: UUID,
    issuer: str,
    deadline: datetime,
    paid_until: datetime,
) -> AsyncIterator[CredentialAuthority]:
    service = await _locked(db, SyncService, SyncService.service_id == service_id)
    client = await _locked(db, SyncCredentialClient, SyncCredentialClient.client_id == client_id)
    key = await _locked(db, SyncSigningKey, SyncSigningKey.id == key_id)
    installation = await _locked(db, SyncInstallation, SyncInstallation.id == installation_id)
    if (
        service is None
        or client is None
        or key is None
        or installation is None
        or installation.service_id != service_id
        or client.service_id != service.service_id
        or client_id != service.service_client_id
        or installation.service_client_id != client_id
        or key.client_id != client_id
    ):
        raise SyncCredentialError()
    row = installation
    resource = await _locked(
        db,
        Resource,
        (Resource.id == row.resource_pk)
        & (Resource.workspace_id == row.workspace_id)
        & (Resource.resource_id == row.resource_id),
    )
    context = await _locked(
        db,
        Context,
        (Context.id == row.context_id)
        & (Context.workspace_id == row.workspace_id)
        & (Context.resource_id == row.resource_id),
    )
    endpoint = await _locked(
        db,
        PublicMcpEndpoint,
        (PublicMcpEndpoint.id == row.endpoint_id)
        & (PublicMcpEndpoint.workspace_id == row.workspace_id)
        & (PublicMcpEndpoint.context_id == row.context_id)
        & (PublicMcpEndpoint.resource_pk == row.resource_pk)
        & (PublicMcpEndpoint.resource_id == row.resource_id),
    )
    if (
        resource is None
        or context is None
        or endpoint is None
        or not row.operations
        or not set(row.operations) <= set(SYNC_OPERATIONS)
        or key_fingerprint(key.public_jwk) != key.fingerprint
    ):
        raise SyncCredentialError()
    authority = CredentialAuthority(
        service, client, key, row, resource, context, endpoint, issuer, (), deadline, paid_until
    )
    authority.snapshot = authority.identity()
    authority.check()
    event.listen(db.sync_session, "before_commit", authority.check)
    try:
        yield authority
    finally:
        event.remove(db.sync_session, "before_commit", authority.check)


def _audit(db: AsyncSession, authority: CredentialAuthority, action: str) -> None:
    db.add(
        AuditLog(
            user_id="service:" + authority.client.client_id,
            user_email="system:sync-client",
            action=action,
            resource="sync:" + str(authority.installation.id),
            user_metadata={
                "workspace_id": str(authority.installation.workspace_id),
                "service_id": authority.service.service_id,
                "client_id": authority.client.client_id,
                "key_id": str(authority.key.id),
                "generation": authority.installation.generation,
            },
        )
    )


async def issue_token(db: AsyncSession, form: dict[str, str]) -> dict[str, Any]:
    """Consume one client assertion and atomically issue paid installation authority."""
    if (
        set(form)
        != {
            "grant_type",
            "client_id",
            "client_assertion_type",
            "client_assertion",
            "scope",
            "resource",
        }
        or form["grant_type"] != "client_credentials"
        or form["client_assertion_type"] != ASSERTION_TYPE
    ):
        raise SyncCredentialError("invalid_request", 400)
    if form["scope"] != "resource.sync":
        raise SyncCredentialError("invalid_scope", 400)
    issuer = sync_issuer()
    try:
        installation_id = UUID(form["resource"].rsplit("/", 1)[-1])
        if form["resource"] != resource_uri(issuer, installation_id):
            raise ValueError()
    except ValueError:
        raise SyncCredentialError("invalid_target", 400) from None
    try:
        assertion_header(form["client_assertion"])
    except InvalidAssertion:
        raise SyncCredentialError("invalid_client", 400) from None
    await _clean_session(db)
    proof = await _authenticate(db, form, issuer)
    await db.rollback()  # release the plain-read connection before the replay receipt
    await _consume_assertion(db, proof)
    initial = await db.get(SyncInstallation, installation_id)
    if initial is None or initial.service_id != proof.service_id:
        raise SyncCredentialError("invalid_target", 400)
    workspace_id = initial.workspace_id
    async with AsyncExitStack() as fences:
        async with paid_integration_write(db, workspace_id, proof.service_id) as paid:
            if paid.valid_until is None:
                raise SyncCredentialError()
            authority = await fences.enter_async_context(
                _authority(
                    db,
                    installation_id,
                    proof.service_id,
                    proof.assertion.client_id,
                    proof.key_id,
                    issuer,
                    proof.assertion.expires_at,
                    paid.valid_until,
                )
            )
            if (
                authority.installation.workspace_id != workspace_id
                or authority.service.generation != proof.service_generation
                or authority.client.generation != proof.client_generation
                or authority.key.fingerprint != proof.fingerprint
            ):
                raise SyncCredentialError("invalid_client", 400)
            value = "kmc_sync_at_" + secrets.token_urlsafe(32)
            row = authority.installation
            now = utcnow()
            db.add(
                SyncAccessToken(
                    token_hash=hashlib.sha256(value.encode()).hexdigest(),
                    installation_id=row.id,
                    workspace_id=row.workspace_id,
                    service_id=row.service_id,
                    client_id=authority.client.client_id,
                    key_id=authority.key.id,
                    key_fingerprint=authority.key.fingerprint,
                    service_generation=authority.service.generation,
                    client_generation=authority.client.generation,
                    installation_generation=row.generation,
                    resource_pk=row.resource_pk,
                    context_id=row.context_id,
                    endpoint_id=row.endpoint_id,
                    resource_id=row.resource_id,
                    operations=list(row.operations),
                    issuer=issuer,
                    resource=resource_uri(issuer, row.id),
                    created_at=now,
                    expires_at=now + timedelta(seconds=300),
                )
            )
            _audit(db, authority, "sync.token.issued")
        return {
            "access_token": value,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "resource.sync",
        }


@asynccontextmanager
async def sync_token_boundary(
    db: AsyncSession, value: str, installation_id: UUID, *, operation: str
) -> AsyncIterator[CredentialAuthority]:
    """Required boundary for every sync consumer, owning admission through commit."""
    if not TOKEN_PATTERN.fullmatch(value):
        raise SyncCredentialError()
    issuer = sync_issuer()
    await _clean_session(db)
    digest = hashlib.sha256(value.encode()).hexdigest()
    initial = await db.get(SyncAccessToken, digest)
    if initial is None or initial.installation_id != installation_id:
        raise SyncCredentialError()
    wid, sid, client_id, key_id, expires_at = (
        initial.workspace_id,
        initial.service_id,
        initial.client_id,
        initial.key_id,
        initial.expires_at,
    )
    async with AsyncExitStack() as fences:
        async with paid_integration_write(db, wid, sid) as paid:
            if paid.valid_until is None:
                raise SyncCredentialError()
            authority = await fences.enter_async_context(
                _authority(
                    db,
                    installation_id,
                    sid,
                    client_id,
                    key_id,
                    issuer,
                    expires_at,
                    paid.valid_until,
                )
            )
            token = await _locked(db, SyncAccessToken, SyncAccessToken.token_hash == digest)
            row = authority.installation
            if (
                row.workspace_id != wid
                or row.service_id != sid
                or token is None
                or token.installation_id != installation_id
            ) or (
                (
                    token.workspace_id,
                    token.service_id,
                    token.client_id,
                    token.key_id,
                    token.key_fingerprint,
                    token.service_generation,
                    token.client_generation,
                    token.installation_generation,
                    token.resource_pk,
                    token.context_id,
                    token.endpoint_id,
                    token.resource_id,
                    token.operations,
                    token.issuer,
                    token.resource,
                    token.scope,
                )
                != (
                    row.workspace_id,
                    row.service_id,
                    authority.client.client_id,
                    authority.key.id,
                    authority.key.fingerprint,
                    authority.service.generation,
                    authority.client.generation,
                    row.generation,
                    row.resource_pk,
                    row.context_id,
                    row.endpoint_id,
                    row.resource_id,
                    row.operations,
                    issuer,
                    resource_uri(issuer, row.id),
                    "resource.sync",
                )
            ):
                raise SyncCredentialError()
            authority.token = token
            authority.token_snapshot = authority.token_identity()
            authority.check()
            if operation not in token.operations:
                raise SyncCredentialError("insufficient_scope", 403)
            yield authority
