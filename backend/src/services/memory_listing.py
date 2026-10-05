"""Deterministic memory reads beside ``recall`` (#1852): ``list`` and ``changes_since``.

``recall`` ranks and caps; these two lanes do neither. ``list`` returns every
live memory of one context that matches exact filters, ordered and paged;
``changes_since`` returns the context's memory-level change log — created,
updated, superseded, forgotten — in time order with a keyset cursor; every
event of a forgotten memory is listed without its summary (#1876). Plain SQL:
no embedding, no Hebbian write, so both are cheap enough for a session start.

Scoping is the REST list's: the caller must read the context; in a private
context only the owner's rows (identity-link aware, ``owned_by``) are visible;
the agent-binding predicate applies as everywhere else.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnElement

from models.auth import CONTEXT_TRUST_TIER_TRUSTED, Context
from models.memory import EDGE_TYPE_SUPERSEDES, SOURCE_TYPE_CONNECTOR, Memory, NeuralMemoryEdge
from services.agent_binding_service import binding_memory_sql_predicate
from services.identity_link_service import owned_by
from utils.datetime import parse_iso8601_to_aware, to_utc_iso

ORDER_COLUMNS = {
    "updated_at": func.coalesce(Memory.updated_at, Memory.created_at),
    "created_at": Memory.created_at,
    "importance": Memory.importance,
}
DIRECTIONS = ("asc", "desc")
MAX_LIMIT = 100
DEFAULT_LIMIT = 50
KINDS = ("created", "updated", "superseded", "forgotten")
# Widest continuation token the change log emits: base64 of "<iso>|<kind>|<uuid>"
# (~90 characters). Envelopes that append next_cursor after measuring reserve it.
CHANGE_CURSOR_RESERVE = 96

_DETAILS_KEY = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SEPARATORS_SQL = "[[:space:]_-]+"
_SEPARATORS_PY = r"[ \t\n\r\f\v_-]+"  # POSIX [[:space:]] spelled out, same class
_IMPORTANCE_OPS = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}
_TIME_FILTERS = {
    "created_after": (Memory.created_at, ">="),
    "created_before": (Memory.created_at, "<"),
    "updated_after": (Memory.updated_at, ">="),
    "updated_before": (Memory.updated_at, "<"),
}
_KNOWN_FILTERS = {
    "type",
    "scope",
    "source_type",
    "delivery_mode",
    "tags",
    "tags_match",
    "tags_normalize",
    "importance",
    "source_uri_prefix",
    "trust_tier",
    *_TIME_FILTERS,
}
_RECALL_ONLY = {"near", "within"}


def to_naive_utc(dt: datetime) -> datetime:
    """An aware datetime as the naive UTC the ``memories`` timestamps are stored in."""
    return dt.astimezone(UTC).replace(tzinfo=None)


def _naive_utc(value: Any, name: str) -> datetime:
    """ISO 8601 (naive = UTC, or aware) → naive UTC, the storage convention."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 string")
    aware = parse_iso8601_to_aware(value, name)
    return aware.astimezone(UTC).replace(tzinfo=None)


def _one_or_many(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    raise ValueError(f"{name} must be a string or a non-empty list of strings")


def _normalized_needle(tag: str) -> str:
    """Fold a tag the way ``_SEPARATORS_SQL`` + ``lower()`` fold the stored ones.

    The two sides must agree character for character: ``str.lower`` (not
    ``casefold``, which expands ``ß`` to ``ss`` while Postgres ``lower`` keeps
    it) and the same separator class as the SQL pattern.
    """
    return re.sub(_SEPARATORS_PY, "", tag.lower())


def _trusted_predicate() -> ColumnElement[bool]:
    return and_(
        Memory.context_id.in_(
            select(Context.id).where(Context.trust_tier == CONTEXT_TRUST_TIER_TRUSTED)
        ),
        Memory.source_type != SOURCE_TYPE_CONNECTOR,
    )


def _details_equals(sub: str, value: bool | int | float | str) -> ColumnElement[bool]:
    """``details.<sub>`` equals the scalar ``value``: same JSON type, same text.

    The stored value must have the filter value's JSON type, so the string
    ``"true"`` does not match the boolean ``true`` and the string ``"2"`` does
    not match the number ``2``. A string is compared unescaped. A number is
    compared as it is written in the stored JSON (``details`` is a ``json``
    column, which keeps the text): ``2`` matches ``2`` but not ``2.0``, and
    ``2.0`` matches ``2.0`` but not ``2``. A missing key, ``null``, an object
    or an array never matches.
    """
    stored = Memory.details[sub]
    if isinstance(value, bool):  # before int: bool is an int subclass
        json_type, text = "boolean", "true" if value else "false"
    elif isinstance(value, str):
        json_type, text = "string", value
    else:
        json_type, text = "number", json.dumps(value)  # the spelling the column was written with
    return and_(func.json_typeof(stored) == json_type, stored.as_string() == text)


def compile_memory_filters(filters: dict[str, Any] | None) -> list[ColumnElement[bool]]:
    """Translate recall's filter vocabulary into SQL predicates on ``Memory``.

    Supported: type, scope, source_type, delivery_mode (string or list → IN);
    tags with tags_match any|all and tags_normalize (case, hyphen / underscore /
    space insensitive — the plural tolerance recall has is not applied here);
    importance {gte, lte, gt, lt}; created_/updated_ after/before (ISO 8601,
    after inclusive, before exclusive); source_uri_prefix; trust_tier='trusted';
    ``details.<key>`` equality for a scalar value (see ``_details_equals``: same
    JSON type, and for a number the same spelling). ``near`` / ``within`` are
    recall_nearby's job and are refused by name. ``updated_after`` / ``updated_before`` read the
    ``updated_at`` column and therefore never match a never-edited memory, while
    ``order_by="updated_at"`` falls back to ``created_at`` for those rows.

    Raises:
        ValueError: an unknown key, a wrong type or an unsupported filter —
            caller errors, surfaced as validation_error.
    """
    if filters is None:
        return []
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    out: list[ColumnElement[bool]] = []
    tags = None
    tags_match = "any"
    tags_match_given = False
    tags_normalize = False
    for key, value in filters.items():
        if key in _RECALL_ONLY:
            raise ValueError(f"{key} is not supported by list; use recall_nearby for places")
        if key.startswith("details."):
            sub = key[len("details.") :]
            if not _DETAILS_KEY.match(sub):
                raise ValueError("details.<key> must match ^[A-Za-z0-9_]{1,64}$")
            if not isinstance(value, (bool, int, float, str)):
                raise ValueError(f"{key} must be a string, number or boolean")
            out.append(_details_equals(sub, value))
            continue
        if key not in _KNOWN_FILTERS:
            raise ValueError(f"unknown filter {key!r}")
        if key in ("type", "scope", "source_type", "delivery_mode"):
            out.append(getattr(Memory, key).in_(_one_or_many(value, key)))
        elif key == "tags":
            tags = _one_or_many(value, "tags")
        elif key == "tags_match":
            if value not in ("any", "all"):
                raise ValueError("tags_match must be 'any' or 'all'")
            tags_match = value
            tags_match_given = True
        elif key == "tags_normalize":
            if not isinstance(value, bool):
                raise ValueError("tags_normalize must be a boolean")
            tags_normalize = value
        elif key == "importance":
            if not isinstance(value, dict) or not value:
                raise ValueError("importance must be an object with gte/lte/gt/lt")
            for op, bound in value.items():
                if (
                    op not in _IMPORTANCE_OPS
                    or isinstance(bound, bool)  # True would silently read as 1.0
                    or not isinstance(bound, (int, float))
                ):
                    raise ValueError("importance accepts gte/lte/gt/lt with numbers")
                out.append(Memory.importance.op(_IMPORTANCE_OPS[op])(float(bound)))
        elif key in _TIME_FILTERS:
            column, op = _TIME_FILTERS[key]
            out.append(column.op(op)(_naive_utc(value, key)))
        elif key == "source_uri_prefix":
            if not isinstance(value, str) or not value:
                raise ValueError("source_uri_prefix must be a non-empty string")
            out.append(Memory.source_uri.startswith(value, autoescape=True))
        elif key == "trust_tier":
            if value != "trusted":
                raise ValueError("trust_tier accepts 'trusted' only")
            out.append(_trusted_predicate())
    if (tags_match_given or tags_normalize) and not tags:
        raise ValueError("tags_match / tags_normalize need a tags filter to apply to")
    if tags:
        if tags_normalize:
            needles = [_normalized_needle(t) for t in tags]
            tag = func.unnest(Memory.tags).column_valued("t")
            folded = func.regexp_replace(func.lower(tag), _SEPARATORS_SQL, "", "g")

            def exists_for(needle: str) -> ColumnElement[bool]:
                return select(literal(1)).where(folded == needle).exists()

            if tags_match == "all":
                out.extend(exists_for(n) for n in needles)
            else:
                out.append(or_(*(exists_for(n) for n in needles)))
        else:
            arr = cast(tags, Memory.tags.type)
            out.append(
                Memory.tags.contains(arr) if tags_match == "all" else Memory.tags.overlap(arr)
            )
    return out


@dataclass(frozen=True)
class ListPage:
    rows: list[Memory]
    total: int
    has_more: bool


async def list_memories(
    db: AsyncSession,
    *,
    context_id: UUID,
    owner_user_id: str | None,
    filters: dict[str, Any] | None = None,
    predicates: list[ColumnElement[bool]] | None = None,
    order_by: str = "updated_at",
    direction: str = "desc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> ListPage:
    """Every live memory of ``context_id`` matching ``filters``, ordered and paged.

    ``owner_user_id`` is the caller in a private context (identity-link aware)
    and ``None`` in a shared one. Order is ``(order_by, id)`` so pages are
    stable; ``total`` is the full match count for the same predicates, read in
    its own statement — under concurrent writes an offset page can shift, which
    is inherent to offset paging (``changes_since`` has the keyset cursor).
    """
    if order_by not in ORDER_COLUMNS:
        raise ValueError(f"order_by must be one of {', '.join(ORDER_COLUMNS)}")
    if direction not in DIRECTIONS:
        raise ValueError("direction must be 'asc' or 'desc'")
    where: list[Any] = [Memory.deleted_at.is_(None), Memory.context_id == context_id]
    if owner_user_id is not None:
        where.append(owned_by(Memory.user_id, owner_user_id))
    binding = await binding_memory_sql_predicate(db)
    if binding is not None:
        where.append(binding)
    # The handler compiles before it opens a session, so a bad filter costs no
    # database round trip; other callers may still pass the raw dict.
    where.extend(predicates if predicates is not None else compile_memory_filters(filters))
    column = ORDER_COLUMNS[order_by]
    order = (
        (column.asc(), Memory.id.asc()) if direction == "asc" else (column.desc(), Memory.id.desc())
    )
    total = (await db.execute(select(func.count()).select_from(Memory).where(*where))).scalar_one()
    rows = (
        (
            await db.execute(
                select(Memory).where(*where).order_by(*order).offset(offset).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return ListPage(rows=list(rows[:limit]), total=int(total), has_more=len(rows) > limit)


@dataclass(frozen=True)
class Change:
    memory_id: UUID
    kind: str
    at: datetime
    summary: str | None  # None: the memory is forgotten (soft-deleted)
    superseded_by: UUID | None


@dataclass(frozen=True)
class ChangePage:
    changes: list[Change]
    next_cursor: str | None


def change_item(c: Change) -> dict[str, Any]:
    """One change as every envelope renders it (``changes_since`` and ``bootstrap``).

    ``summary`` is omitted for an event of a forgotten memory, ``superseded_by``
    for every kind but ``superseded`` (the omit-empty response convention).
    """
    return {
        "memory_id": str(c.memory_id),
        "kind": c.kind,
        "at": to_utc_iso(c.at),
        **({"summary": c.summary} if c.summary is not None else {}),
        **({"superseded_by": str(c.superseded_by)} if c.superseded_by else {}),
    }


def encode_change_cursor(change: Change) -> str:
    raw = f"{change.at.isoformat()}|{change.kind}|{change.memory_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_change_cursor(cursor: str) -> tuple[datetime, str, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at_s, kind, mid = base64.urlsafe_b64decode(padded.encode()).decode().split("|")
        if kind not in KINDS:
            raise ValueError
        return datetime.fromisoformat(at_s), kind, UUID(mid)
    except Exception as e:  # any malformed token is a caller error
        raise ValueError(
            "cursor must be the next_cursor of a previous changes_since response"
        ) from e


async def changes_since(
    db: AsyncSession,
    *,
    context_id: UUID,
    owner_user_id: str | None,
    since: datetime,
    until: datetime | None,
    kinds: tuple[str, ...] = KINDS,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    trusted_only: bool = False,
) -> ChangePage:
    """The context's memory-level change log in ``[since, until)``, oldest first.

    created: ``created_at`` in the window. updated: ``updated_at`` in the window
    and later than ``created_at`` — an edit, a scope promotion or a Sleep
    maintenance change (importance re-evaluation, dedup tag merge, rollback);
    never the initial write or an embedding retry: the embedding pipeline keeps
    its own clock (``embedding_attempted_at``) and does not stamp ``updated_at``.
    superseded: a ``supersedes`` edge in the window whose live source is the
    newer memory — ``at`` is the edge's ``created_at``, which the upsert
    re-dates when an existing edge of another type becomes ``supersedes``, so
    a supersession declared over an older semantic link is dated when it was
    declared; ``superseded_by`` is the source id.
    forgotten: ``deleted_at`` in the window, listed while the tombstone exists;
    a forgotten row's earlier created / updated / superseded events stay in the
    log too. Every event of a forgotten memory, whatever its kind, carries
    ``memory_id`` / ``kind`` / ``at`` only: ``summary`` is ``None`` (#1876), so
    ``forget`` retracts the text from this lane as it does from the others
    while the log keeps its shape.
    Keyset cursor over ``(at, kind, id)``, so a page is never shifted by rows
    written after it was read.
    """
    for kind in kinds:
        if kind not in KINDS:
            raise ValueError(f"kinds must be a subset of {', '.join(KINDS)}")
    if until is not None and until <= since:
        raise ValueError("until must be later than since")
    common: list[Any] = [Memory.context_id == context_id]
    if owner_user_id is not None:
        common.append(owned_by(Memory.user_id, owner_user_id))
    if trusted_only:
        common.append(_trusted_predicate())
    binding = await binding_memory_sql_predicate(db)
    if binding is not None:
        common.append(binding)

    def window(column: Any) -> list[Any]:
        preds = [column >= since]
        if until is not None:
            preds.append(column < until)
        return preds

    parts = []
    none_uuid = literal(None).cast(Memory.id.type)
    # NULL for a soft-deleted row, cast so every part of the union has one type.
    live_summary = case(
        (Memory.deleted_at.is_not(None), literal(None).cast(Memory.summary.type)),
        else_=Memory.summary,
    )

    def event(kind: str, at: Any, superseded_by: Any) -> Any:
        # Every part labels its columns: the union takes its names from the
        # first part, and any kind may be the first when ``kinds`` is narrowed.
        return select(
            Memory.id.label("memory_id"),
            literal(kind).label("kind"),
            at.label("at"),
            live_summary.label("summary"),
            superseded_by.label("superseded_by"),
        )

    if "created" in kinds:
        parts.append(
            event("created", Memory.created_at, none_uuid).where(
                *common, *window(Memory.created_at)
            )
        )
    if "updated" in kinds:
        parts.append(
            event("updated", Memory.updated_at, none_uuid).where(
                *common,
                Memory.updated_at.is_not(None),
                Memory.updated_at > Memory.created_at,
                *window(Memory.updated_at),
            )
        )
    if "superseded" in kinds:
        newer = aliased(Memory)
        parts.append(
            event("superseded", NeuralMemoryEdge.created_at, NeuralMemoryEdge.src_id)
            .select_from(NeuralMemoryEdge)
            .join(Memory, Memory.id == NeuralMemoryEdge.dst_id)
            .join(newer, newer.id == NeuralMemoryEdge.src_id)
            .where(
                *common,
                NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
                newer.deleted_at.is_(None),
                newer.context_id == context_id,  # edges are intra-context; never leak a foreign id
                *window(NeuralMemoryEdge.created_at),
            )
        )
    if "forgotten" in kinds:
        parts.append(
            event("forgotten", Memory.deleted_at, none_uuid).where(
                *common, Memory.deleted_at.is_not(None), *window(Memory.deleted_at)
            )
        )
    if not parts:
        return ChangePage(changes=[], next_cursor=None)
    u = union_all(*parts).subquery("changes") if len(parts) > 1 else parts[0].subquery("changes")
    key = tuple_(u.c.at, cast(u.c.kind, String), u.c.memory_id)
    query = select(u.c.memory_id, u.c.kind, u.c.at, u.c.summary, u.c.superseded_by)
    if cursor:
        at, kind, mid = decode_change_cursor(cursor)
        query = query.where(key > tuple_(literal(at), literal(kind), literal(mid)))
    query = query.order_by(u.c.at.asc(), u.c.kind.asc(), u.c.memory_id.asc()).limit(limit + 1)
    rows = (await db.execute(query)).all()
    changes = [
        Change(
            memory_id=r.memory_id,
            kind=r.kind,
            at=r.at,
            summary=r.summary,
            superseded_by=r.superseded_by,
        )
        for r in rows[:limit]
    ]
    next_cursor = encode_change_cursor(changes[-1]) if len(rows) > limit and changes else None
    return ChangePage(changes=changes, next_cursor=next_cursor)
