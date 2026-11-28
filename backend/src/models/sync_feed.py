"""Staged feed snapshots, canonical approved records and durable index handoff."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from db.base import Base
from models.json_types import NullableJSONB
from utils.datetime import utcnow


class SyncFeed(Base):
    """Immutable kind and versioned source configuration within one installation."""

    __tablename__ = "sync_feeds"
    __table_args__ = (
        # f68 (#3055): the kinds of every registered record schema version.
        CheckConstraint("kind IN ('product', 'faq', 'page', 'review')", name="ck_sync_feed_kind"),
        CheckConstraint("revision >= 0", name="ck_sync_feed_revision"),
        CheckConstraint("config_version > 0", name="ck_sync_feed_config_version"),
        CheckConstraint("length(config_hash) = 64", name="ck_sync_feed_config_hash"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncRun(Base):
    """A durable operation receipt; credentials are deliberately absent from its identity.

    ``change_count`` is the length of the diff the run was given (#2916): it is
    set with every diff written through the ORM and stays when an aborted,
    expired or superseded run's ``diff`` is cleared, so a run's change count
    never depends on the diff still being stored. NULL until the first diff.
    """

    __tablename__ = "sync_runs"
    __table_args__ = (
        UniqueConstraint("feed_id", "operation_id", name="uq_sync_run_operation"),
        CheckConstraint(
            "state IN ('uploading', 'prepared', 'committed', 'aborted', 'expired')",
            name="ck_sync_run_state",
        ),
        CheckConstraint("base_revision >= 0", name="ck_sync_run_revision"),
        CheckConstraint("expires_at > created_at", name="ck_sync_run_expiry"),
        CheckConstraint("length(request_hash) = 64", name="ck_sync_run_request_hash"),
        # #2916 (f55): the expiry and retention sweeps select by state and age;
        # the superseded-diff pass reads only committed runs still holding one.
        Index("ix_sync_runs_state_expires_at", "state", "expires_at"),
        Index(
            "ix_sync_runs_committed_diff",
            "committed_at",
            postgresql_where=text("state = 'committed' AND diff IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    feed_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_feeds.id", ondelete="CASCADE"), index=True
    )
    operation_id: Mapped[UUID] = mapped_column(Uuid())
    request_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    generations: Mapped[list[int]] = mapped_column(JSONB)
    base_revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="uploading")
    diff: Mapped[list[dict] | None] = mapped_column(NullableJSONB)
    change_count: Mapped[int | None] = mapped_column(Integer)
    diff_hash: Mapped[str | None] = mapped_column(String(64))
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    prepared_until: Mapped[datetime | None] = mapped_column(DateTime)
    approval_id: Mapped[UUID | None] = mapped_column(Uuid())
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )
    approval_epoch: Mapped[int | None] = mapped_column(Integer)
    commit_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)

    @validates("diff")
    def _count_changes(self, _key: str, diff: list[dict] | None) -> list[dict] | None:
        """Every stored diff sets the count; clearing the diff keeps it."""
        if diff is not None:
            self.change_count = len(diff)
        return diff

    @property
    def manual_request_id(self) -> UUID | None:
        """Immutable non-secret provenance survives finite request retention."""
        value = self.manifest.get("manual_request_id")
        return UUID(value) if isinstance(value, str) else None


class SyncChunk(Base):
    """Finite staging content; immutable digest receipts survive payload purging."""

    __tablename__ = "sync_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0 AND chunk_index < 1000", name="ck_sync_chunk_index"),
        CheckConstraint("byte_count > 0 AND byte_count <= 524288", name="ck_sync_chunk_bytes"),
        CheckConstraint("length(digest) = 64", name="ck_sync_chunk_digest"),
        CheckConstraint("record_count > 0 AND record_count <= 100", name="ck_sync_chunk_records"),
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int] = mapped_column(Integer)
    record_count: Mapped[int] = mapped_column(Integer)
    records: Mapped[list[dict] | None] = mapped_column(NullableJSONB)


class SyncRecord(Base):
    """Canonical field allowlist, with stable tombstones preventing version reuse."""

    __tablename__ = "sync_records"
    __table_args__ = (
        UniqueConstraint("feed_id", "external_id", name="uq_sync_record_external"),
        UniqueConstraint("resource_pk", "doc_id", name="uq_sync_record_document"),
        CheckConstraint("version > 0", name="ck_sync_record_version"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    feed_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_feeds.id", ondelete="CASCADE"), index=True
    )
    resource_pk: Mapped[UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(200))
    doc_id: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(NullableJSONB)
    last_event_id: Mapped[int] = mapped_column(
        ForeignKey("resource_events.id", ondelete="CASCADE"), index=True
    )


class SyncIndexOutbox(Base):
    """Committed events and their target are enqueued in the same SQL transaction."""

    __tablename__ = "sync_index_outbox"
    __table_args__ = (
        # #2916 (f55): the replay and the live batch's receipt lock read only
        # undelivered receipts.
        Index(
            "ix_sync_index_outbox_undelivered",
            "resource_pk",
            "context_id",
            "last_event_id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    resource_pk: Mapped[UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    context_id: Mapped[UUID] = mapped_column(
        ForeignKey("contexts.id", ondelete="CASCADE"), index=True
    )
    last_event_id: Mapped[int] = mapped_column(BigInteger)
    event_ids: Mapped[list[int]] = mapped_column(JSONB)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
