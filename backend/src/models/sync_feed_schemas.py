"""Strict, bounded wire contracts for full feed snapshots and their record schemas.

An installation's record schema is the latest ``resource_schemas`` row of its
resource: the pair (``schema_version``, ``digest(field_definitions)``). Every
validation, comparison and answer follows that stored pair through
``stored_schema`` and the registry below; a pair the registry does not hold is
refused (409) wherever it is read, and nothing repairs it.
"""

import copy
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from models.management_schemas import digest
from models.sync_feed_rules import (
    ATTRIBUTES_LIMITS,
    FAQ,
    FAQ_REQUIRED,
    FIELDS,
    FIELDS_V2,
    INDEXED,
    INDEXED_V2,
    MAX_RECORD_BYTES,
    PAGE_V2,
    PAGE_V2_REQUIRED,
    PRODUCT,
    PRODUCT_REQUIRED,
    PRODUCT_V2,
    REVIEW_V2,
    REVIEW_V2_REQUIRED,
    RULES_V1,
    RULES_V2,
    FieldRule,
    check_record,
)
from utils.query_bounds import MAX_INT32

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_RECORDS = 10_000
MAX_CHUNK_BYTES = 524_288
MAX_RUN_BYTES = 20 * 1024 * 1024
# Every kind of any registered version; an installation accepts its own version's.
FeedKind = Literal["product", "faq", "page", "review"]


def definitions(names: frozenset[str], indexed: frozenset[str]) -> list[dict[str, Any]]:
    """The stored definitions of a version: five keys each, in code-point order of the names."""
    return [
        {
            "name": name,
            "type": "text",
            "classification": "public",
            "required": name in {"record_type", "external_id"},
            "index_hint": "fulltext,vector" if name in indexed else "",
        }
        for name in sorted(names)
    ]


DEFINITIONS = definitions(FIELDS, INDEXED)
SCHEMA_HASH = digest(DEFINITIONS)
# The ``limits`` of every ``/schema`` answer, in wire order.
BASE_LIMITS: dict[str, int] = {
    "feeds": 20,
    "records": MAX_RECORDS,
    "chunks": 1000,
    "chunk_records": 100,
    "chunk_bytes": MAX_CHUNK_BYTES,
    "run_bytes": MAX_RUN_BYTES,
    "run_ttl_seconds": 86400,
    "approval_ttl_seconds": 900,
}


@dataclass(frozen=True, eq=False)
class SyncSchema:
    """One registered record schema version: its definitions, kinds and published limits.

    ``kinds`` maps each kind, in wire order, to the keys a record's ``fields``
    may hold and the keys it must hold. ``extra_limits`` follow the base
    limits in the ``/schema`` answer.
    """

    version: int
    definitions: list[dict[str, Any]]
    kinds: Mapping[str, tuple[frozenset[str], frozenset[str]]]
    rules: Mapping[str, FieldRule]
    extra_limits: Mapping[str, Any] = field(default_factory=dict)
    schema_hash: str = field(init=False)
    names: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_hash", digest(self.definitions))
        object.__setattr__(self, "names", frozenset(d["name"] for d in self.definitions))

    def stored_definitions(self) -> list[dict[str, Any]]:
        """A copy of the definitions for a new ``resource_schemas`` row."""
        return copy.deepcopy(self.definitions)

    def answer(self) -> dict[str, Any]:
        """The worker ``/schema`` answer of an installation that stores this version."""
        return {
            "schema_hash": self.schema_hash,
            "schema_version": self.version,
            "fields": self.stored_definitions(),
            "kinds": list(self.kinds),
            "limits": {**BASE_LIMITS, **copy.deepcopy(dict(self.extra_limits))},
        }


SCHEMA_V1 = SyncSchema(
    version=1,
    definitions=DEFINITIONS,
    kinds={
        "product": (frozenset(PRODUCT), frozenset(PRODUCT_REQUIRED)),
        "faq": (frozenset(FAQ), frozenset(FAQ_REQUIRED)),
    },
    rules=RULES_V1,
)
# Version 2 (#3055): pages and reviews, ten more product fields. Its /schema
# answer also publishes the record and field limits (version 1's never does).
SCHEMA_V2 = SyncSchema(
    version=2,
    definitions=definitions(FIELDS_V2, INDEXED_V2),
    kinds={
        "product": (PRODUCT_V2, frozenset(PRODUCT_REQUIRED)),
        "faq": (frozenset(FAQ), frozenset(FAQ_REQUIRED)),
        "page": (PAGE_V2, PAGE_V2_REQUIRED),
        "review": (REVIEW_V2, REVIEW_V2_REQUIRED),
    },
    rules=RULES_V2,
    extra_limits={
        "record_bytes": MAX_RECORD_BYTES,
        "field_chars": {name: rule.limit for name, rule in sorted(RULES_V2.items())},
        "attributes": dict(ATTRIBUTES_LIMITS),
    },
)
# (schema_version, schema_hash) -> the registered schema.
REGISTRY: dict[tuple[int, str], SyncSchema] = {
    (entry.version, entry.schema_hash): entry for entry in (SCHEMA_V1, SCHEMA_V2)
}


# Every registered hash: a manifest naming any other hash is malformed.
REGISTERED_HASHES = frozenset(schema_hash for _version, schema_hash in REGISTRY)


class StoredSchemaRow(Protocol):
    """The two columns of a ``resource_schemas`` row the registry reads."""

    schema_version: int
    field_definitions: list[dict[str, Any]]


def stored_schema(row: StoredSchemaRow | None) -> SyncSchema | None:
    """The registered schema of a stored row, or ``None`` (no row, unregistered pair)."""
    if row is None:
        return None
    return REGISTRY.get((row.schema_version, digest(row.field_definitions)))


def registered_kinds() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Per kind, the union over every registered version of its allowed and required keys."""
    result: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for entry in REGISTRY.values():
        for kind, (allowed, required) in entry.kinds.items():
            known_allowed, known_required = result.get(kind, (frozenset(), required))
            result[kind] = (known_allowed | allowed, known_required & required)
    return result


class StrictBody(BaseModel):
    """Unknown keys cannot silently become publication instructions."""

    model_config = ConfigDict(extra="forbid", strict=True)


class FeedConfigExpected(StrictBody):
    """One complete snapshot; version zero is an assertion that the feed is absent."""

    config_version: Annotated[StrictInt, Field(ge=0, le=MAX_INT32)]
    generation: Annotated[StrictInt, Field(ge=1, le=MAX_INT32)]
    schema_hash: Hash | None
    schema_version: Annotated[StrictInt, Field(ge=1, le=MAX_INT32)] | None

    @model_validator(mode="after")
    def complete_schema(self) -> Self:
        """An absent schema has neither an identity hash nor a version."""
        if (self.schema_hash is None) != (self.schema_version is None):
            raise ValueError("incomplete_schema_snapshot")
        return self


class FeedConfig(StrictBody):
    """Opaque hash of the source configuration; KMC never retrieves feed URLs.

    ``kind`` names any registered kind; the installation's stored schema (or
    the schema a first configuration stores) decides whether it is accepted.
    """

    kind: FeedKind
    config_hash: Hash
    # Omission alone preserves the legacy unguarded protocol. SkipJsonSchema
    # describes the accepted optional object, not the internal omission sentinel.
    expected: FeedConfigExpected | SkipJsonSchema[None] = Field(default_factory=lambda: None)

    @model_validator(mode="before")
    @classmethod
    def explicit_null_is_not_omission(cls, value: Any) -> Any:
        """A malformed requested guard must never silently become a legacy write."""
        if isinstance(value, dict) and "expected" in value and value["expected"] is None:
            raise ValueError("invalid_configuration_precondition")
        return value


class RunManifest(StrictBody):
    """The operation identity binds every declared source and snapshot property."""

    feed_id: UUID
    operation_id: UUID
    config_hash: Hash
    source_hash: Hash
    config_version: Annotated[StrictInt, Field(ge=1)]
    schema_version: Annotated[StrictInt, Field(ge=1)]
    schema_hash: Hash
    snapshot_hash: Hash
    chunks: Annotated[StrictInt, Field(ge=0, le=1000)]
    records: Annotated[StrictInt, Field(ge=0, le=MAX_RECORDS)]
    manual_request_id: UUID | None = None


class FeedRecord(StrictBody):
    """Normalized external identity and only explicit public string fields."""

    external_id: Annotated[str, Field(min_length=1, max_length=200)]
    fields: dict[str, str]

    @field_validator("external_id")
    @classmethod
    def stable_id(cls, value: str) -> str:
        """Reject ambiguous IDs instead of silently merging normalization variants."""
        if (
            value != value.strip()
            or unicodedata.normalize("NFC", value) != value
            or len(value.encode()) > 512
            or any(unicodedata.category(c).startswith("C") for c in value)
        ):
            raise ValueError("invalid_external_id")
        return value

    def public_payload(self, kind: str, schema: SyncSchema) -> dict[str, str]:
        """Validate kind-specific fields and values under the installation's stored schema.

        Raises :class:`models.sync_feed_rules.RecordRefusal` (a ``ValueError``)
        with the first failing rule; a kind outside the schema is a plain
        ``ValueError``.
        """
        if kind not in schema.kinds:
            raise ValueError("invalid_fields")
        allowed, required = schema.kinds[kind]
        return check_record(
            kind,
            self.external_id,
            self.fields,
            allowed=allowed,
            required=required,
            rules=schema.rules,
            names=schema.names,
        )


class ChunkBody(StrictBody):
    """One retryable, position-addressed upload chunk."""

    records: Annotated[list[FeedRecord], Field(min_length=1, max_length=100)]


class ConfirmDiff(StrictBody):
    """Exact prepared diff, with separate human approval for destructive changes."""

    diff_hash: Hash
    approval_id: UUID | None = None


def snapshot_digest(records: list[dict]) -> str:
    """Canonical digest independent of upload chunk boundaries or order."""
    return digest(sorted(records, key=lambda row: row["external_id"].encode("utf-8")))


def destructive(diff: list[dict], previous_count: int) -> bool:
    """All-delete, 100 deletions, or at least 20 percent requires human consent."""
    count = sum(item["op"] == "delete" for item in diff)
    return count > 0 and (count >= 100 or count * 5 >= previous_count)
