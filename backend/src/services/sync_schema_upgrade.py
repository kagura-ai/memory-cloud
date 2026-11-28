"""Upgrade one installation's record schema from version 1 to version 2 (#3055).

A Context Editor with ``integrations:manage`` (the authority that approves the
schema at feed configuration) appends the version-2 schema row. In order,
under the human feed boundary's locks (workspace row ``FOR UPDATE NOWAIT``):

1. no stored schema, or a stored pair the registry does not hold: 409;
2. already on version 2: 200 with ``changed: false``, whatever ``expected``
   says, and nothing is written;
3. ``expected`` differs from the stored pair and the installation generation:
   409;
4. an active run of the installation (``sync_feed_runs.active_run_count``):
   409 ``sync_schema_upgrade_runs_active``;
5. otherwise, in one transaction: the version-2 schema row, the resume
   candidates made inactive with their records dropped, the resume preview
   deleted, the audit action ``sync.schema.upgraded``.

The generation is not bumped (worker credentials stay valid); feeds and
stored records are untouched. A manual request and a records cursor are bound
to the stored schema pair, so they end. There is no way back to version 1.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from models.resource import ResourceSchema
from models.sync_feed_schemas import SCHEMA_V2, SchemaUpgrade, stored_schema
from models.sync_installation import SyncInstallation
from models.sync_management import SyncResumeCandidate, SyncResumePreview
from services.sync_feed_authority import current_schema, human_feed_boundary
from services.sync_feed_runs import active_run_count
from utils.exceptions import ConflictError, SyncSchemaUpgradeRunsActive


def _answer(installation: SyncInstallation, *, changed: bool) -> dict:
    return {
        "schema_hash": SCHEMA_V2.schema_hash,
        "schema_version": SCHEMA_V2.version,
        "generation": installation.generation,
        "changed": changed,
    }


async def upgrade_schema(
    db: AsyncSession, wid: UUID, iid: UUID, principal: SyncPrincipal, body: SchemaUpgrade
) -> dict:
    """Store record schema version 2 for one installation, once; see the module docstring."""
    async with human_feed_boundary(db, wid, iid, principal, write=True) as (
        installation,
        _workspace,
        _fence,
    ):
        schema = await current_schema(db, installation)
        entry = stored_schema(schema)
        if schema is None or entry is None:
            raise ConflictError("Synchronization schema cannot be upgraded")
        if entry is SCHEMA_V2:
            await db.commit()
            return _answer(installation, changed=False)
        expected = body.expected
        if (expected.schema_version, expected.schema_hash, expected.generation) != (
            entry.version,
            entry.schema_hash,
            installation.generation,
        ):
            raise ConflictError("Synchronization schema snapshot changed")
        if await active_run_count(db, installation.id) > 0:
            raise SyncSchemaUpgradeRunsActive()
        db.add(
            ResourceSchema(
                resource_pk=installation.resource_pk,
                resource_id=installation.resource_id,
                schema_version=SCHEMA_V2.version,
                field_definitions=SCHEMA_V2.stored_definitions(),
            )
        )
        for candidate in (
            await db.scalars(
                select(SyncResumeCandidate).where(
                    SyncResumeCandidate.installation_id == installation.id,
                    SyncResumeCandidate.active,
                )
            )
        ).all():
            candidate.active, candidate.records = False, None
        preview = await db.get(SyncResumePreview, installation.id)
        if preview is not None:
            await db.delete(preview)
        from services.sync_audit import add_audit, human_actor

        add_audit(
            db,
            "sync.schema.upgraded",
            human_actor(principal.user_id, principal.client_id),
            installation=installation,
        )
        await db.flush()
        await db.commit()
        return _answer(installation, changed=True)
