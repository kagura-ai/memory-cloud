"""Operator-set prices for ``llm_pricing`` (#1570).

``llm_pricing`` is append-only and, until now, only Alembic wrote it. A
deployment that points ``provider="self_hosted"`` at a *paid* OpenAI-compatible
endpoint therefore had no way to price its models: cost rendered as ``$0`` /
unknown and the embedding spend cap never fired.

``LLM_PRICING_OVERRIDES`` is a JSON **array** of objects::

    [{"provider": "self_hosted", "model": "qwen3-embedding:4b",
      "unit_type": "embedding_tokens", "price_per_unit": 0.02}]

Keys: ``provider`` and ``model`` (the REGISTRY model name — what usage rows
record, not the aliased wire id), ``unit_type`` (one of
``LLM_PRICING_UNIT_TYPES``), ``price_per_unit`` (USD, ``>= 0``, at most
``MAX_PRICE_DECIMALS`` decimal places — the column's scale), optional
``unit_denominator`` (``> 0``, default 1,000,000 — i.e. price per million) and
optional ``context_min_tokens`` (``>= 0``, default 0). Currency is always USD;
convert a vendor's non-USD price before setting it.

Unlike ``SELF_HOSTED_MODEL_ALIASES`` this parser is **strict**: a malformed
value raises ``ValueError`` naming the variable and the offending entry, and
the Settings validator turns that into a refused boot — a silently dropped
price would leave a paid endpoint uncapped, which is the bug this fixes.
The parsed tuple is materialized into the table by
``services.llm_pricing_service.sync_llm_pricing_overrides``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

ENV_NAME = "LLM_PRICING_OVERRIDES"
DEFAULT_UNIT_DENOMINATOR = 1_000_000
# ``llm_pricing.price_per_unit`` is ``Numeric(14, 10)``. Postgres would round an
# 11th decimal HALF_UP on INSERT, so the stored price would no longer equal the
# configured one and ``sync_llm_pricing_overrides`` would see a "different" row
# and append another on every boot. Refuse it instead of rounding silently.
MAX_PRICE_DECIMALS = 10

_REQUIRED_KEYS = frozenset({"provider", "model", "unit_type", "price_per_unit"})
_OPTIONAL_KEYS = frozenset({"unit_denominator", "context_min_tokens"})


@dataclass(frozen=True)
class PricingOverride:
    """One validated ``LLM_PRICING_OVERRIDES`` entry."""

    provider: str
    model: str
    unit_type: str
    price_per_unit: Decimal
    unit_denominator: int = DEFAULT_UNIT_DENOMINATOR
    context_min_tokens: int = 0

    @property
    def lookup_key(self) -> tuple[str, str, str, int]:
        """The tuple the effective-row lookup keys on (sans ``effective_from``)."""
        return (self.provider, self.model, self.unit_type, self.context_min_tokens)


def _error(index: int, message: str) -> ValueError:
    return ValueError(f"{ENV_NAME}: entry {index} {message}")


def _non_empty_str(entry: dict[str, Any], key: str, index: int) -> str:
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise _error(index, f"has an invalid {key!r} (non-empty string required)")
    return value.strip()


def _non_negative_int(entry: dict[str, Any], key: str, index: int, *, minimum: int) -> int:
    value = entry[key]
    # ``bool`` is an ``int`` subclass; refuse it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(index, f"has an invalid {key!r} (integer >= {minimum} required)")
    return value


def _price(entry: dict[str, Any], index: int) -> Decimal:
    value = entry["price_per_unit"]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _error(index, "has an invalid 'price_per_unit' (number >= 0 required)")
    try:
        # ``Decimal(str(x))`` keeps the operator's decimal literal exact;
        # ``Decimal(0.1)`` would carry the binary-float expansion.
        price = Decimal(str(value).strip())
    except InvalidOperation:
        raise _error(index, "has an invalid 'price_per_unit' (number >= 0 required)") from None
    if not price.is_finite() or price < 0:
        raise _error(index, "has an invalid 'price_per_unit' (number >= 0 required)")
    # ``normalize()`` first so ``"0.0200000000000"`` (trailing zeros) still passes;
    # the exponent of a finite Decimal is ``-<decimal places>`` (the ``isinstance``
    # only narrows the NaN/Infinity literals ``is_finite()`` already excluded).
    exponent = price.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -MAX_PRICE_DECIMALS:
        raise _error(
            index,
            f"has an invalid 'price_per_unit' (at most {MAX_PRICE_DECIMALS} decimal places)",
        )
    return price


def _parse_entry(entry: Any, index: int, unit_types: tuple[str, ...]) -> PricingOverride:
    if not isinstance(entry, dict):
        raise _error(index, "is not a JSON object")
    keys = set(entry)
    missing = sorted(_REQUIRED_KEYS - keys)
    if missing:
        raise _error(index, f"is missing required key(s): {', '.join(missing)}")
    unknown = sorted(keys - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if unknown:
        raise _error(index, f"has unknown key(s): {', '.join(unknown)}")

    unit_type = _non_empty_str(entry, "unit_type", index)
    if unit_type not in unit_types:
        raise _error(
            index,
            f"has an invalid 'unit_type' {unit_type!r}; must be one of {', '.join(unit_types)}",
        )
    return PricingOverride(
        provider=_non_empty_str(entry, "provider", index),
        model=_non_empty_str(entry, "model", index),
        unit_type=unit_type,
        price_per_unit=_price(entry, index),
        unit_denominator=(
            _non_negative_int(entry, "unit_denominator", index, minimum=1)
            if "unit_denominator" in entry
            else DEFAULT_UNIT_DENOMINATOR
        ),
        context_min_tokens=(
            _non_negative_int(entry, "context_min_tokens", index, minimum=0)
            if "context_min_tokens" in entry
            else 0
        ),
    )


def parse_llm_pricing_overrides(raw: str | None) -> tuple[PricingOverride, ...]:
    """Parse and validate ``LLM_PRICING_OVERRIDES``.

    Args:
        raw: The env value as read by Settings. Blank means no overrides.

    Returns:
        The validated entries in input order.

    Raises:
        ValueError: Malformed JSON, a non-array top level, or an entry with a
            missing / unknown key, bad ``unit_type``, negative or over-precise
            price, bad denominator, or a duplicate ``(provider, model, unit_type,
            context_min_tokens)``. The message names ``LLM_PRICING_OVERRIDES``
            and the zero-based entry index.
    """
    if raw is None or not raw.strip():
        return ()
    # Imported here, not at module top: ``config.settings`` imports this module,
    # and ``models`` (the package ``models.llm_pricing`` lives in) pulls in
    # modules that import ``config.settings`` back — a top-level import would
    # be a cycle. Blank values (the default) never pay for the import.
    from models.llm_pricing import LLM_PRICING_UNIT_TYPES

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_NAME} is not valid JSON: {exc.msg}") from None
    if not isinstance(data, list):
        raise ValueError(f"{ENV_NAME} must be a JSON array of objects")

    overrides: list[PricingOverride] = []
    seen: dict[tuple[str, str, str, int], int] = {}
    for index, entry in enumerate(data):
        override = _parse_entry(entry, index, LLM_PRICING_UNIT_TYPES)
        first = seen.setdefault(override.lookup_key, index)
        if first != index:
            raise _error(
                index,
                f"is a duplicate of entry {first} "
                "(same provider, model, unit_type and context_min_tokens)",
            )
        overrides.append(override)
    return tuple(overrides)
