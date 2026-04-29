"""Shared base classes for API response models.

`TZAwareBaseModel` ensures every `datetime` field on a response model is
serialized to ISO 8601 with `Z` for UTC datetimes, while non-UTC offsets are
preserved as `+HH:MM`, so JS clients parse the result unambiguously instead
of local time. The DB stores naive UTC (TIMESTAMP WITHOUT TIME ZONE) for now;
until the column-type migration in #490 lands, this base class is the
API-layer guarantee that wire-format datetimes are unambiguous.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_serializer

from utils.datetime import to_utc_iso


class TZAwareBaseModel(BaseModel):
    """Base model that serializes naive `datetime` fields with a Z suffix.

    Inherit from this instead of `pydantic.BaseModel` for any response schema
    that exposes a `datetime` field. Non-datetime fields are unaffected — they
    pass through Pydantic's default JSON serializer via the wrap handler.

    Subclasses that declare their own `@field_serializer("created_at", ...)`
    override this wildcard for that specific field (Pydantic v2 behavior:
    field-specific serializers win). Prefer letting this base class handle
    datetime fields and removing per-field serializers, so the project has a
    single canonical pattern.
    """

    @field_serializer("*", mode="wrap", when_used="json", check_fields=False)
    def _serialize_datetime_as_utc(self, value: Any, handler: Callable[[Any], Any]) -> Any:
        """Serialize datetime fields with an explicit UTC `Z` suffix.

        Fires on every field during JSON serialization (`when_used="json"`).
        For non-datetime values, delegates to Pydantic's default serializer
        via `handler` so the rest of the schema serializes normally.

        Args:
            value: Field value being serialized.
            handler: Default Pydantic serializer for non-datetime field types.

        Returns:
            For datetime input: ISO 8601 string with `Z` suffix (naive treated
            as UTC, `+00:00` normalized to `Z`, non-UTC offsets preserved).
            For all other types: the default JSON-serialized form.
        """
        if isinstance(value, datetime):
            return to_utc_iso(value)
        return handler(value)
