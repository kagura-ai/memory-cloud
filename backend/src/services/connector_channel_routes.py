"""Per-channel destination contexts of a chat connector (#1759).

A connector writes every ingested channel into its default context
(``workspace_connectors.context_id``). A row of
``workspace_connector_channel_routes`` gives one channel a destination of its
own: ``route`` to a context, or ``skip`` (do not ingest it). The connector's
``unrouted_private_channels`` switch says what happens to a private channel
without a row: ``default`` (today's behaviour) or ``skip``.

Fail closed, in every reader of this module:

* a channel with a route row never falls back to the default context. A
  ``route`` whose context is gone (hard-deleted: ``context_id`` SET NULL),
  soft-deleted, or outside the connector's workspace is a MISSING
  DESTINATION and is vended to the worker as ``skip`` until an admin picks a
  new one;
* an unknown stored ``action`` or switch value is ``skip``;
* a worker that does not declare the ``channel_routes`` capability is never
  served the config of a connector that uses routing (``uses_routing``).

The connector's KMC key is a workspace-wide key that acts as its owner, so a
route is validated at save time against what that owner may write
(``ConnectorProvisioningService.update_connector_settings``); nothing here
restricts the key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.exceptions import ValidationError

# The token a worker sends in ``GET /workers/config?capabilities=`` to say it
# implements the routing contract (docs/connector-ingest-contract.md).
CHANNEL_ROUTES_CAPABILITY = "channel_routes"

ROUTE = "route"
SKIP = "skip"
ROUTE_ACTIONS = (ROUTE, SKIP)
UNROUTED_DEFAULT = "default"
UNROUTED_SKIP = "skip"
UNROUTED_VALUES = (UNROUTED_DEFAULT, UNROUTED_SKIP)

# Same bounds as ``channel_ids`` (connector_provisioning._MAX_CHANNEL_IDS /
# _MAX_CHANNEL_ID_CHARS): Slack ids are ~11 chars, the caps only keep a
# hostile payload from bloating the table and the worker config body.
MAX_CHANNEL_ROUTES = 500
MAX_CHANNEL_ID_CHARS = 64

ROUTING_DISABLED_MESSAGE = "channel routing is not enabled on this deployment"

RouteStatus = Literal["ok", "destination_missing"]
_FIELD = "channel_routes"


@dataclass(frozen=True)
class ChannelRouteInput:
    """One validated item of a ``channel_routes`` PATCH."""

    channel_id: str
    action: str
    context_id: UUID | None


@dataclass(frozen=True)
class ChannelRouteView:
    """One stored route as the admin reads it (settings result, connector list).

    ``context_name`` / ``context_is_private`` are set only while the
    destination is live in the connector's workspace; ``context_id`` is
    withheld for a context outside it.
    """

    channel_id: str
    action: str
    context_id: UUID | None
    context_name: str | None
    context_is_private: bool | None
    status: RouteStatus


def _item_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def parse_channel_routes(raw: Any) -> list[ChannelRouteInput]:
    """Validate the shape of a ``channel_routes`` PATCH (no database).

    ``None`` and ``[]`` both mean "no routes" (the full replacement removes
    every stored one). Each item is ``{channel_id, action, context_id}``:
    ``action`` defaults to ``route``, a ``route`` needs a ``context_id`` and a
    ``skip`` must not carry one; channel ids follow the ``channel_ids`` rule and
    appear at most once.

    Raises:
        ValidationError: on ``channel_routes``, naming the channel id at fault
            (never a context).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("channel_routes must be a list (or null to clear)", field=_FIELD)
    if len(raw) > MAX_CHANNEL_ROUTES:
        raise ValidationError(
            f"channel_routes holds at most {MAX_CHANNEL_ROUTES} channels", field=_FIELD
        )
    parsed: list[ChannelRouteInput] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) and not hasattr(item, "channel_id"):
            raise ValidationError(
                "each channel_routes item is an object {channel_id, action, context_id}",
                field=_FIELD,
            )
        channel_id = _item_value(item, "channel_id")
        if (
            not isinstance(channel_id, str)
            or not channel_id.strip()
            or len(channel_id) > MAX_CHANNEL_ID_CHARS
        ):
            raise ValidationError(
                "channel_routes: channel_id must be a non-blank channel id string "
                f"({MAX_CHANNEL_ID_CHARS} chars max)",
                field=_FIELD,
            )
        if channel_id in seen:
            raise ValidationError(
                f"channel_routes: channel {channel_id} appears more than once", field=_FIELD
            )
        seen.add(channel_id)
        action = _item_value(item, "action", ROUTE) or ROUTE
        if action not in ROUTE_ACTIONS:
            raise ValidationError(
                f"channel_routes: channel {channel_id} has an unknown action "
                "(use 'route' or 'skip')",
                field=_FIELD,
            )
        raw_context = _item_value(item, "context_id")
        if action == SKIP:
            if raw_context is not None:
                raise ValidationError(
                    f"channel_routes: channel {channel_id} is 'skip' and takes no context_id",
                    field=_FIELD,
                )
            parsed.append(ChannelRouteInput(channel_id, SKIP, None))
            continue
        if raw_context is None:
            raise ValidationError(
                f"channel_routes: channel {channel_id} is 'route' and needs a context_id",
                field=_FIELD,
            )
        try:
            context_id = raw_context if isinstance(raw_context, UUID) else UUID(str(raw_context))
        except ValueError as exc:
            raise ValidationError(
                f"channel_routes: channel {channel_id} has a malformed context_id",
                field=_FIELD,
            ) from exc
        parsed.append(ChannelRouteInput(channel_id, ROUTE, context_id))
    return parsed


def parse_capabilities(raw: str | None) -> frozenset[str]:
    """The tokens of a comma-separated ``capabilities`` value; unknown ones are
    kept here and simply never looked up."""
    if not raw:
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip())


def effective_unrouted(value: object) -> str:
    """The stored switch as it is acted on: anything but ``default`` is skip."""
    return UNROUTED_DEFAULT if value == UNROUTED_DEFAULT else UNROUTED_SKIP


def _destination_live(row: Any, workspace_id: UUID) -> bool:
    """A ``route`` row's context exists, is not soft-deleted and is in the
    connector's workspace."""
    return (
        row.action == ROUTE
        and row.context_id is not None
        and row.context_workspace_id is not None
        and row.context_workspace_id == workspace_id
        and row.context_deleted_at is None
    )


@dataclass(frozen=True)
class EffectiveRouting:
    """What the worker acts on for one connector.

    ``routes`` is ``(channel_id, action, context_id)`` sorted by channel id,
    ``context_id`` set only on a live ``route``.
    """

    routes: tuple[tuple[str, str, UUID | None], ...]
    unrouted_private_channels: str

    @property
    def uses_routing(self) -> bool:
        """At least one route row, or private channels without one skipped."""
        return bool(self.routes) or self.unrouted_private_channels != UNROUTED_DEFAULT

    def vend_routes(self) -> list[dict[str, Any]]:
        """The ``channel_routes`` list of the worker config."""
        vended: list[dict[str, Any]] = []
        for channel_id, action, context_id in self.routes:
            if action == ROUTE and context_id is not None:
                vended.append({"channel_id": channel_id, "action": ROUTE, "context_id": context_id})
            else:
                vended.append({"channel_id": channel_id, "action": SKIP})
        return vended

    def digest(self) -> str:
        """A stable digest of the effective routes and the switch, for the
        worker config revision: a destination deleted later changes it although
        ``config_version`` did not move."""
        material = json.dumps(
            {
                "routes": [
                    [channel_id, action, str(context_id) if context_id else None]
                    for channel_id, action, context_id in self.routes
                ],
                "unrouted_private_channels": self.unrouted_private_channels,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode()).hexdigest()


NO_ROUTING = EffectiveRouting(routes=(), unrouted_private_channels=UNROUTED_DEFAULT)


def effective_routing_from_rows(
    rows: Iterable[Any], workspace_id: UUID, unrouted_value: object
) -> EffectiveRouting:
    """Fold stored route rows (joined with their context) into what is vended."""
    routes: list[tuple[str, str, UUID | None]] = []
    for row in rows:
        if _destination_live(row, workspace_id):
            routes.append((row.channel_id, ROUTE, row.context_id))
        else:
            routes.append((row.channel_id, SKIP, None))
    routes.sort(key=lambda r: r[0])
    return EffectiveRouting(
        routes=tuple(routes), unrouted_private_channels=effective_unrouted(unrouted_value)
    )


def route_views_from_rows(rows: Iterable[Any], workspace_id: UUID) -> list[ChannelRouteView]:
    """Stored route rows as the admin reads them, sorted by channel id."""
    views: list[ChannelRouteView] = []
    for row in rows:
        if row.action != ROUTE:
            views.append(ChannelRouteView(row.channel_id, SKIP, None, None, None, "ok"))
            continue
        if _destination_live(row, workspace_id):
            views.append(
                ChannelRouteView(
                    row.channel_id,
                    ROUTE,
                    row.context_id,
                    row.context_name,
                    bool(row.context_is_private),
                    "ok",
                )
            )
            continue
        in_workspace = row.context_id is not None and row.context_workspace_id == workspace_id
        views.append(
            ChannelRouteView(
                row.channel_id,
                ROUTE,
                row.context_id if in_workspace else None,
                None,
                None,
                "destination_missing",
            )
        )
    views.sort(key=lambda v: v.channel_id)
    return views


async def _route_rows(db: AsyncSession, connector_ids: Collection[UUID]) -> Sequence[Any]:
    """Every route row of ``connector_ids`` with its context's columns (outer
    join: a hard-deleted destination has no context row). Read-only."""
    from models.auth import Context
    from models.resource import WorkspaceConnectorChannelRoute as Route

    if not connector_ids:
        return []
    result = await db.execute(
        select(
            Route.connector_id,
            Route.channel_id,
            Route.action,
            Route.context_id,
            Context.workspace_id.label("context_workspace_id"),
            Context.deleted_at.label("context_deleted_at"),
            # NULL-only fallback, as the connector list names the default context.
            func.coalesce(Context.display_name, Context.name).label("context_name"),
            Context.is_private.label("context_is_private"),
        )
        .outerjoin(Context, Context.id == Route.context_id)
        .where(Route.connector_id.in_(list(connector_ids)))
        .order_by(Route.connector_id, Route.channel_id)
    )
    return result.all()


async def route_views(
    db: AsyncSession, connectors: Iterable[tuple[UUID, UUID]]
) -> dict[UUID, list[ChannelRouteView]]:
    """``{connector_id: views}`` for ``(connector_id, workspace_id)`` pairs, in
    one query. A connector without routes maps to ``[]``."""
    pairs = dict(connectors)
    grouped: dict[UUID, list[Any]] = {connector_id: [] for connector_id in pairs}
    for row in await _route_rows(db, list(pairs)):
        grouped.setdefault(row.connector_id, []).append(row)
    return {
        connector_id: route_views_from_rows(rows, pairs[connector_id])
        for connector_id, rows in grouped.items()
        if connector_id in pairs
    }


async def effective_channel_routing(db: AsyncSession, connector: Any) -> EffectiveRouting:
    """What the worker acts on for ``connector`` (one query)."""
    rows = await _route_rows(db, [connector.id])
    return effective_routing_from_rows(
        rows, connector.workspace_id, connector.unrouted_private_channels
    )


async def stored_routes(db: AsyncSession, connector_id: UUID) -> set[tuple[str, str, UUID | None]]:
    """The stored ``(channel_id, action, context_id)`` set of one connector, as
    written (no effective reading), to tell whether a PATCH changed it."""
    from models.resource import WorkspaceConnectorChannelRoute as Route

    result = await db.execute(
        select(Route.channel_id, Route.action, Route.context_id).where(
            Route.connector_id == connector_id
        )
    )
    return {(row.channel_id, row.action, row.context_id) for row in result.all()}
