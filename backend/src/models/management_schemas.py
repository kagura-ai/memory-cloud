"""Strict management contract. FieldDefinition metadata is not an ACL."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Operation = Literal[
    "schema:read",
    "events:upsert",
    "events:delete",
    "tokens:read",
    "tokens:create",
    "tokens:update",
    "tokens:revoke",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrantCreate(StrictModel):
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    operations: list[Operation] = Field(min_length=1, max_length=7)
    approved_fields: list[str] = Field(min_length=1, max_length=100)
    ttl_days: StrictInt = Field(default=30, ge=1, le=90)
    quota_events_per_hour: StrictInt = Field(default=1000, ge=1, le=10000)
    max_active_tokens: StrictInt = Field(default=3, ge=1, le=100)
    acknowledge_full_resource_token_authority: bool = False

    @model_validator(mode="after")
    def delegation(self) -> GrantCreate:
        if (
            "tokens:create" in self.operations
            and not self.acknowledge_full_resource_token_authority
        ):
            raise ValueError("Token creation requires explicit full-resource delegation")
        if len(set(self.approved_fields)) != len(self.approved_fields) or len(
            set(self.operations)
        ) != len(self.operations):
            raise ValueError("Duplicate permission")
        return self


class ManagedEvent(StrictModel):
    op: Literal["upsert", "delete"]
    doc_id: str = Field(min_length=1, max_length=255)
    version: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    payload: dict[str, Any] | None = None
    idempotency_key: UUID

    @model_validator(mode="after")
    def valid_operation(self) -> ManagedEvent:
        if self.op == "upsert" and (not self.payload or self.version is None):
            raise ValueError("Upsert requires payload and integer version")
        if self.op == "delete" and self.payload is not None:
            raise ValueError("Delete does not accept payload")
        if len(json.dumps(self.payload, ensure_ascii=False).encode()) > 100000:
            raise ValueError("Payload exceeds limit")
        return self


class ManagedBatch(StrictModel):
    operation_id: UUID
    schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    events: list[ManagedEvent] = Field(min_length=1, max_length=100)


class ManagedToken(StrictModel):
    operation_id: UUID
    description: str = Field(default="", max_length=500)
    quota_events_per_hour: StrictInt = Field(default=1000, ge=1, le=10000)


class ManagedRevoke(StrictModel):
    operation_id: UUID


def digest(value: Any) -> str:
    """Stable digest for revision checks and idempotency, never a secret hash."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def public_fields(definitions: list[dict], approved: list[str]) -> list[dict]:
    """Explicit owner approval AND public metadata; fail closed on drift."""
    names = {f.get("name") for f in definitions}
    if len(names) != len(definitions) or not set(approved).issubset(names):
        raise ValueError("Invalid approved fields")
    result = []
    for field in definitions:
        if field.get("name") not in approved:
            if field.get("required"):
                raise ValueError("Required field not approved")
            continue
        if field.get("classification") != "public" or field.get("type") not in {
            "text",
            "number",
            "boolean",
            "date",
        }:
            raise ValueError("Unapproved or unsupported field")
        # Do not disclose internal descriptions, examples, or arbitrary metadata.
        result.append(
            {"name": field["name"], "type": field["type"], "required": field.get("required", False)}
        )
    return result


def validate_public_payload(payload: dict, fields: list[dict]) -> None:
    """A small supported scalar contract, independently enforced upstream."""
    allowed = {field["name"]: field for field in fields}
    if not set(payload).issubset(allowed):
        raise ValueError("Unknown field")
    for name, field in allowed.items():
        if name not in payload:
            if field["required"]:
                raise ValueError("Required field missing")
            continue
        value, kind = payload[name], field["type"]
        if kind in {"text", "date"} and not isinstance(value, str):
            raise ValueError("Expected text")
        if kind == "boolean" and type(value) is not bool:
            raise ValueError("Expected boolean")
        if kind == "number":
            try:
                finite = type(value) in {int, float} and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("Expected finite number")
        if kind == "date":
            assert isinstance(value, str)
            try:
                date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError("Expected ISO date") from exc
