"""Strict, bounded wire contracts for full product/FAQ feed snapshots."""

import json
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from models.management_schemas import digest

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_RECORDS = 10_000
MAX_CHUNK_BYTES = 524_288
MAX_RUN_BYTES = 20 * 1024 * 1024
PRODUCT = {
    "title",
    "url",
    "description",
    "image_url",
    "price",
    "currency",
    "availability",
    "brand",
    "sku",
    "gtin",
    "mpn",
    "condition",
}
FAQ = {"question", "answer", "url"}
FIELDS = PRODUCT | FAQ | {"record_type", "external_id"}
DEFINITIONS = [
    {
        "name": name,
        "type": "text",
        "classification": "public",
        "required": name in {"record_type", "external_id"},
        "index_hint": "fulltext,vector"
        if name in {"title", "description", "question", "answer", "brand", "sku"}
        else "",
    }
    for name in sorted(FIELDS)
]
SCHEMA_HASH = digest(DEFINITIONS)


class StrictBody(BaseModel):
    """Unknown keys cannot silently become publication instructions."""

    model_config = ConfigDict(extra="forbid", strict=True)


class FeedConfig(StrictBody):
    """Opaque hash of the source configuration; KMC never retrieves feed URLs."""

    kind: Literal["product", "faq"]
    config_hash: Hash


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

    def public_payload(self, kind: str) -> dict[str, str]:
        """Validate kind-specific fields and values before they enter staging."""
        allowed = PRODUCT if kind == "product" else FAQ
        required = {"title", "url"} if kind == "product" else {"question", "answer"}
        if not required.issubset(self.fields) or not set(self.fields).issubset(allowed):
            raise ValueError("invalid_fields")
        for name, value in self.fields.items():
            limit = 16_000 if name in {"description", "answer"} else 2048
            if (
                not value.strip()
                or len(value) > limit
                or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
            ):
                raise ValueError("invalid_field_value")
            if name in {"url", "image_url"}:
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or any(c.isspace() for c in value)
                ):
                    raise ValueError("invalid_url")
        if ("price" in self.fields) != ("currency" in self.fields):
            raise ValueError("price_currency_pair_required")
        if "price" in self.fields and not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?", self.fields["price"]
        ):
            raise ValueError("invalid_price")
        if "currency" in self.fields and not re.fullmatch(r"[A-Z]{3}", self.fields["currency"]):
            raise ValueError("invalid_currency")
        for name, choices in {
            "availability": {"in_stock", "out_of_stock", "preorder", "backorder"},
            "condition": {"new", "used", "refurbished"},
        }.items():
            if name in self.fields and self.fields[name] not in choices:
                raise ValueError("invalid_enum")
        payload = {"record_type": kind, "external_id": self.external_id, **self.fields}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 32768:
            raise ValueError("record_too_large")
        return payload


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
