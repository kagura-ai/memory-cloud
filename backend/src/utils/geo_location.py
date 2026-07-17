"""``details.location`` contract + geo query helpers (pure logic, no I/O).

The WHERE axis (#1331) mirrors the Time Memory shape: caller-supplied
structured data inside ``details`` drives STORED generated columns
(``location_lat`` / ``location_lon``) that a deterministic query lane
(``recall_nearby``) filters on. ``normalize_location`` is the single
write-side validator (called from every write path via
``MemoryService._apply_location``); the query-side helpers validate
``recall_nearby`` arguments to the same standard and derive the bbox
prefilter that keeps the partial index usable.

Contract (spec ``docs/design/2026-07-17-location-axis-design.md`` §4):

    details.location = {
        "lat":   number (required, -90..90),
        "lon":   number (required, -180..180),
        "label": string (optional, ≤256 chars),
        "text":  string (optional, pass-through),
    }

Rejections are deliberate 422s, not silent NULLs: MCP argument coercion does
not recurse into ``details``, so a string ``"35.6"`` would otherwise store
fine, fail the generated column's numeric regex guard, and leave the memory
invisible to ``recall_nearby`` with no error anywhere.
"""

from __future__ import annotations

import math
from typing import Any

LAT_MIN = -90.0
LAT_MAX = 90.0
LON_MIN = -180.0
LON_MAX = 180.0

# recall_nearby clamps (mirrors clamp_upcoming_k for the time lane).
DEFAULT_NEARBY_K = 20
MAX_NEARBY_K = 100
DEFAULT_RADIUS_M = 1000.0
MIN_RADIUS_M = 1.0
MAX_RADIUS_M = 1_000_000.0  # 1000 km

# Rounded write-back precision: 7 decimal places ≈ 1 cm at the equator.
_COORD_DECIMALS = 7

# Mean Earth radius — the single sphere both the bbox prefilter (here) and
# the SQL haversine (services/geo_memory.py) are derived from. A prefilter
# must be a SUPERSET of the exact filter: deriving meters-per-degree from a
# different constant (e.g. the WGS84 equatorial 111,320 m/deg) makes the
# latitude window ~0.11% narrower than the haversine's reach and silently
# drops rows in the annulus at the radius edge.
EARTH_RADIUS_M = 6_371_000.0
_METERS_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0  # ≈ 111,194.93

_ALLOWED_KEYS = frozenset({"lat", "lon", "label", "text"})
_LABEL_MAX_CHARS = 256


class LocationValidationError(ValueError):
    """Raised when a caller-supplied location payload is invalid.

    Subclasses ``ValueError`` so the existing MCP ``validation_error`` /
    REST 422 mappings (the ``TriggerValidationError`` wiring) apply as-is.
    """


def _require_coord(value: Any, name: str, lo: float, hi: float) -> float:
    """Validate one coordinate: number-only, finite, in range.

    ``bool`` is an ``int`` subclass — reject it explicitly (the float twin of
    ``_require_int``'s bool-is-int trap in utils/time_trigger.py). String
    numerics are rejected rather than coerced (see module docstring).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocationValidationError(
            f"location.{name} must be a number, got {type(value).__name__}"
        )
    try:
        coord = float(value)
    except OverflowError as exc:
        # An unbounded Python int (JSON integer literal) passes the
        # isinstance gate but overflows float — same clean 422 as any other
        # out-of-domain coordinate, never an unmapped 500.
        raise LocationValidationError(f"location.{name} must be within [{lo}, {hi}]") from exc
    if not math.isfinite(coord):
        raise LocationValidationError(f"location.{name} must be finite")
    if not (lo <= coord <= hi):
        raise LocationValidationError(f"location.{name} must be within [{lo}, {hi}]")
    return round(coord, _COORD_DECIMALS)


def normalize_location(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate ``details.location`` and write the normalized form back.

    Gate: fires only when the ``location`` key is present (the WHERE axis is
    an orthogonal attribute — any memory type may carry it). Absent key or
    ``None`` details pass through untouched. Invalid shapes raise
    :class:`LocationValidationError` (→ 422), never silently store.
    """
    if details is None or "location" not in details:
        return details

    location = details["location"]
    if not isinstance(location, dict):
        raise LocationValidationError(
            "details.location must be an object like "
            '{"lat": 35.68, "lon": 139.76, "label": "optional"}'
        )
    unknown = sorted(set(location) - _ALLOWED_KEYS)
    if unknown:
        raise LocationValidationError(
            f"details.location has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(_ALLOWED_KEYS))})"
        )
    for required in ("lat", "lon"):
        if required not in location:
            raise LocationValidationError(f"details.location requires '{required}'")

    normalized: dict[str, Any] = {
        "lat": _require_coord(location["lat"], "lat", LAT_MIN, LAT_MAX),
        "lon": _require_coord(location["lon"], "lon", LON_MIN, LON_MAX),
    }
    if "label" in location:
        label = location["label"]
        if not isinstance(label, str):
            raise LocationValidationError("location.label must be a string")
        if len(label) > _LABEL_MAX_CHARS:
            raise LocationValidationError(
                f"location.label must be at most {_LABEL_MAX_CHARS} characters"
            )
        normalized["label"] = label
    if "text" in location:
        text = location["text"]
        if not isinstance(text, str):
            raise LocationValidationError("location.text must be a string")
        normalized["text"] = text

    details["location"] = normalized
    return details


def validate_query_coords(lat: Any, lon: Any) -> tuple[float, float]:
    """Validate recall_nearby query coordinates to the write-side standard."""
    return (
        _require_coord(lat, "lat", LAT_MIN, LAT_MAX),
        _require_coord(lon, "lon", LON_MIN, LON_MAX),
    )


def clamp_nearby_k(k: Any) -> int:
    """Clamp result count to [1, 100] (default 20) — clamp_upcoming_k mirror.

    Raises ``ValueError``/``TypeError`` on a non-integer so the handler can
    surface a structured ``validation_error`` (the time-lane contract).
    """
    if k is None:
        return DEFAULT_NEARBY_K
    if isinstance(k, bool):
        # bool is an int — int(True) would silently clamp to 1 result.
        raise LocationValidationError("k must be an integer")
    if isinstance(k, float) and not k.is_integer():
        # int(1.9) would silently truncate; the schema says integer.
        raise LocationValidationError("k must be an integer")
    value = int(k)  # may raise → caller maps to validation_error
    return max(1, min(MAX_NEARBY_K, value))


def clamp_radius_m(radius_m: Any) -> float:
    """Clamp radius to [1 m, 1000 km] (default 1 km).

    Rejects bool and non-finite values (the coordinate rules); numeric
    strings coerce like the time lane's ``int(k)`` does for top-level args.
    Every rejection raises ``LocationValidationError`` (a ValueError) so
    callers get one contract error — no caller-side re-wrapping of the
    bare ``float()`` failure.
    """
    if radius_m is None:
        return DEFAULT_RADIUS_M
    if isinstance(radius_m, bool):
        raise LocationValidationError("radius_m must be a number")
    try:
        value = float(radius_m)
    except (TypeError, ValueError) as e:
        raise LocationValidationError("radius_m must be a number") from e
    if not math.isfinite(value):
        raise LocationValidationError("radius_m must be finite")
    return max(MIN_RADIUS_M, min(MAX_RADIUS_M, value))


def bbox_lat_range(lat: float, radius_m: float) -> tuple[float, float]:
    """Latitude window of the bbox prefilter, clamped to the valid range."""
    half = radius_m / _METERS_PER_DEG_LAT
    return (max(LAT_MIN, lat - half), min(LAT_MAX, lat + half))


def bbox_lon_ranges(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """Longitude window(s) of the bbox prefilter.

    - Mid-latitude: one range, width corrected by ``cos(lat)`` (a degree of
      longitude shrinks toward the poles).
    - Antimeridian (±180°) crossing: two ranges ORed together.
    - Pole neighborhood (the latitude window touches ±90°, or the cos
      correction degenerates): every longitude is within reach — fall back
      to the full span rather than emit a broken window.
    """
    lat_lo, lat_hi = bbox_lat_range(lat, radius_m)
    if lat_hi >= LAT_MAX or lat_lo <= LAT_MIN:
        return [(LON_MIN, LON_MAX)]

    # cos over the window's worst case (closest-to-pole edge) so the box
    # never undershoots the true circle.
    worst_lat = max(abs(lat_lo), abs(lat_hi))
    cos_lat = math.cos(math.radians(worst_lat))
    if cos_lat <= 0.0:
        return [(LON_MIN, LON_MAX)]
    half = radius_m / (_METERS_PER_DEG_LAT * cos_lat)
    if half >= 180.0:
        return [(LON_MIN, LON_MAX)]

    lo = lon - half
    hi = lon + half
    if lo < LON_MIN:
        return [(LON_MIN, hi), (lo + 360.0, LON_MAX)]
    if hi > LON_MAX:
        return [(lo, LON_MAX), (LON_MIN, hi - 360.0)]
    return [(lo, hi)]


_NEAR_ALLOWED_KEYS = frozenset({"lat", "lon", "radius_m"})


def extract_near_filter(filters: dict[str, Any] | None) -> tuple[float, float, float] | None:
    """Extract and validate ``filters["near"]`` (#1332) → ``(lat, lon, radius_m)``.

    The recall ``filters`` namespace is free-form user input where unknown
    keys are ignored, so a PRESENT-and-malformed ``near`` must fail closed
    (#1229 lesson: a silently dropped filter turns a scoped search into an
    unscoped one with no error anywhere). Coordinates are held to the
    write-side standard (``validate_query_coords``) and the radius to the
    ``recall_nearby`` clamps — unknown keys inside ``near`` are rejected
    rather than ignored so a typo like ``radius`` doesn't silently search
    the default 1 km.
    """
    if not filters or "near" not in filters:
        return None
    near = filters["near"]
    if not isinstance(near, dict):
        raise LocationValidationError("near must be an object: {lat, lon, radius_m?}")
    unknown = set(near) - _NEAR_ALLOWED_KEYS
    if unknown:
        raise LocationValidationError(
            f"near has unknown keys: {sorted(unknown)} (allowed: lat, lon, radius_m)"
        )
    if "lat" not in near or "lon" not in near:
        raise LocationValidationError("near requires both lat and lon")
    lat, lon = validate_query_coords(near["lat"], near["lon"])
    radius_m = clamp_radius_m(near.get("radius_m"))
    return lat, lon, radius_m


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters on the shared mean-Earth sphere.

    Python mirror of the SQL haversine in ``services/geo_memory.py`` — the
    LanceDB ``near`` post-filter must draw the same circle as Qdrant's
    server-side ``geo_radius``, and both prefilters derive from the same
    ``EARTH_RADIUS_M``.
    """
    lat1_r, lon1_r, lat2_r, lon2_r = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((lat2_r - lat1_r) / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin((lon2_r - lon1_r) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
