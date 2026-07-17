"""Shared WHERE-axis nearby query (#1331) — ``time_memory.py``'s spatial twin.

Single implementation of the deterministic "memories near a point" read that
the ``recall_nearby`` MCP tool consumes (and any future bootstrap/REST face
would reuse — the ``AgentStateService`` dual-surface pattern). Deterministic
filter+sort over the generated ``location_lat`` / ``location_lon`` columns —
NOT semantic recall, so no Hebbian write side-effects.

Privacy invariant #6 (spec §7): this module never logs — the query point is
precise user location and must not reach server logs or MAE metadata.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.geo_location import bbox_lat_range, bbox_lon_ranges

# Mean Earth radius — pairs with the spherical meters-per-degree constant in
# utils.geo_location (the bbox prefilter and the haversine agree on the sphere).
_EARTH_RADIUS_M = 6_371_000.0


async def query_nearby_memories(
    db: AsyncSession,
    context_id: UUID,
    *,
    lat: float,
    lon: float,
    radius_m: float,
    k: int,
    trusted_only: bool = False,
) -> list[dict[str, Any]]:
    """Return the context's memories within ``radius_m`` of (lat, lon).

    Two-stage plan (spec §6): a bbox prefilter whose predicate repeats the
    partial index's conditions verbatim (``location_lat IS NOT NULL AND
    deleted_at IS NULL`` — a dropped condition surfaces as a seq scan, not a
    silent leak), then an exact SQL haversine that orders by distance and
    drops bbox corners beyond the radius. The longitude window ORs two ranges
    across the ±180° antimeridian and degenerates to the full span near the
    poles (see ``bbox_lon_ranges``).

    ``trusted_only`` mirrors the time lane's two-tier gate (#1293): wired but
    ``False`` for the user-initiated tool; any future bootstrap/always face
    consuming location MUST pass ``True``.

    Result rows: ``memory_id`` / ``summary`` / ``type`` / ``details`` /
    ``distance_m`` (ascending).
    """
    from models.auth import CONTEXT_TRUST_TIER_TRUSTED, Context
    from models.memory import SOURCE_TYPE_CONNECTOR, Memory

    lat_lo, lat_hi = bbox_lat_range(lat, radius_m)
    lon_ranges = bbox_lon_ranges(lat, lon, radius_m)

    # SQL haversine over the generated columns. sin²(Δλ/2) is periodic, so
    # antimeridian-wrapped Δλ needs no special-casing here (the bbox handles
    # index reach; the trig handles correctness). least(1.0, …) guards asin
    # against float drift just past 1.
    dlat_half = func.radians(Memory.location_lat - lat) / 2
    dlon_half = func.radians(Memory.location_lon - lon) / 2
    haversine_a = func.power(func.sin(dlat_half), 2) + func.cos(func.radians(lat)) * func.cos(
        func.radians(Memory.location_lat)
    ) * func.power(func.sin(dlon_half), 2)
    distance_m = (2 * _EARTH_RADIUS_M * func.asin(func.sqrt(func.least(1.0, haversine_a)))).label(
        "distance_m"
    )

    query = (
        select(Memory, distance_m)
        .where(Memory.deleted_at.is_(None))
        .where(Memory.location_lat.isnot(None))
        .where(Memory.context_id == context_id)
        .where(Memory.location_lat.between(lat_lo, lat_hi))
        .where(or_(*[Memory.location_lon.between(lo, hi) for lo, hi in lon_ranges]))
    )
    if trusted_only:
        query = query.where(
            Memory.context_id.in_(
                select(Context.id).where(Context.trust_tier == CONTEXT_TRUST_TIER_TRUSTED)
            )
        ).where(Memory.source_type != SOURCE_TYPE_CONNECTOR)
    query = query.where(distance_m <= radius_m).order_by(distance_m.asc()).limit(k)

    rows = (await db.execute(query)).all()

    # #1299: per-memory type/source binding filter — same subtractive rule as
    # the time lane. Log-only in shadow (outside the MAE operation
    # vocabulary). May underfill k — enforcement is never backfilled.
    from services.agent_binding_service import filter_memory_rows_by_binding

    memories = [row[0] for row in rows]
    distance_by_id = {row[0].id: float(row[1]) for row in rows}
    kept_rows, _ = await filter_memory_rows_by_binding(db, memories, operation=None, user_id=None)
    return [
        {
            "memory_id": str(m.id),
            "summary": m.summary,
            "type": m.type,
            "details": m.details,
            "distance_m": round(distance_by_id[m.id], 1),
        }
        for m in kept_rows
    ]
