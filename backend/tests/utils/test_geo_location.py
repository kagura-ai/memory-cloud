"""Pure-logic tests for utils/geo_location.py (#1331 WHERE axis).

Mirrors tests/utils/test_time_trigger.py's role for the time axis: the
``details.location`` write-side contract (validation + normalization) and the
query-side helpers (coordinate validation, clamps, bbox derivation) with no
DB involved.
"""

from __future__ import annotations

import math

import pytest

from utils.geo_location import (
    LAT_MAX,
    LAT_MIN,
    LON_MAX,
    LON_MIN,
    MAX_RADIUS_M,
    MIN_RADIUS_M,
    LocationValidationError,
    bbox_lon_ranges,
    clamp_nearby_k,
    clamp_radius_m,
    normalize_location,
    validate_query_coords,
)


class TestNormalizeLocation:
    def test_no_location_key_passthrough(self):
        details = {"other": 1}
        assert normalize_location(details) is details

    def test_none_details_passthrough(self):
        assert normalize_location(None) is None

    def test_valid_location_normalized(self):
        details = normalize_location({"location": {"lat": 35.6812, "lon": 139.7671}})
        assert details["location"]["lat"] == 35.6812
        assert details["location"]["lon"] == 139.7671

    def test_rounds_to_seven_decimals(self):
        details = normalize_location({"location": {"lat": 35.68123456789, "lon": -139.76712345678}})
        assert details["location"]["lat"] == 35.6812346
        assert details["location"]["lon"] == -139.7671235

    def test_int_coords_accepted(self):
        details = normalize_location({"location": {"lat": 35, "lon": -139}})
        assert details["location"]["lat"] == 35.0
        assert details["location"]["lon"] == -139.0

    def test_label_and_text_pass_through(self):
        details = normalize_location(
            {"location": {"lat": 0, "lon": 0, "label": "office", "text": "3F meeting room"}}
        )
        assert details["location"]["label"] == "office"
        assert details["location"]["text"] == "3F meeting room"

    @pytest.mark.parametrize("missing", ["lat", "lon"])
    def test_missing_required_key_rejected(self, missing):
        loc = {"lat": 1.0, "lon": 2.0}
        del loc[missing]
        with pytest.raises(LocationValidationError, match=missing):
            normalize_location({"location": loc})

    def test_non_dict_location_rejected(self):
        # Pre-existing free-form values like {"location": "Tokyo office"}
        # must fail loudly (422), not silently produce NULL columns.
        with pytest.raises(LocationValidationError):
            normalize_location({"location": "Tokyo office"})

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_coord_rejected(self, bad):
        # bool is an int subclass — must not be accepted as 1.0/0.0.
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": bad, "lon": 0}})

    def test_string_numeric_rejected(self):
        # MCP arg coercion does not recurse into details — a silently
        # accepted "35.6" would yield NULL generated columns (invisible to
        # recall_nearby). Early 422 is the contract.
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": "35.6", "lon": 139.7}})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_inf_rejected(self, bad):
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": bad, "lon": 0}})

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(90.0000001, 0), (-90.0000001, 0), (0, 180.0000001), (0, -180.0000001)],
    )
    def test_out_of_range_rejected(self, lat, lon):
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": lat, "lon": lon}})

    def test_boundary_values_accepted(self):
        details = normalize_location({"location": {"lat": -90, "lon": 180}})
        assert details["location"]["lat"] == -90.0
        assert details["location"]["lon"] == 180.0

    def test_unknown_key_rejected(self):
        with pytest.raises(LocationValidationError, match="altitude"):
            normalize_location({"location": {"lat": 0, "lon": 0, "altitude": 3.2}})

    def test_non_string_label_rejected(self):
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": 0, "lon": 0, "label": 42}})

    def test_over_long_label_rejected(self):
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": 0, "lon": 0, "label": "x" * 257}})

    def test_non_string_text_rejected(self):
        with pytest.raises(LocationValidationError):
            normalize_location({"location": {"lat": 0, "lon": 0, "text": ["a"]}})

    def test_error_is_value_error(self):
        # The MCP/REST error mapping catches ValueError (time-axis pattern).
        assert issubclass(LocationValidationError, ValueError)


class TestQueryHelpers:
    def test_validate_query_coords_accepts_valid(self):
        lat, lon = validate_query_coords(35.68, 139.76)
        assert (lat, lon) == (35.68, 139.76)

    @pytest.mark.parametrize("bad_lat", [True, "35.6", float("nan"), 90.1])
    def test_validate_query_coords_rejects(self, bad_lat):
        with pytest.raises(LocationValidationError):
            validate_query_coords(bad_lat, 0)

    def test_clamp_k_defaults_and_bounds(self):
        assert clamp_nearby_k(None) == 20
        assert clamp_nearby_k(0) == 1
        assert clamp_nearby_k(1000) == 100
        assert clamp_nearby_k(7) == 7

    def test_clamp_radius_defaults_and_bounds(self):
        assert clamp_radius_m(None) == 1000.0
        assert clamp_radius_m(0) == MIN_RADIUS_M
        assert clamp_radius_m(10_000_000) == MAX_RADIUS_M
        assert clamp_radius_m(250) == 250.0

    def test_bbox_simple_midlatitude(self):
        # 1km at Tokyo: lon half-width ≈ radius / (m-per-deg · cos(lat)),
        # where m-per-deg derives from the SAME sphere as the haversine
        # (π·R/180) — a divergent constant would make the prefilter a strict
        # subset of the exact filter and drop radius-edge rows. Never
        # narrower than the center-lat estimate (worst-case window edge).
        from utils.geo_location import EARTH_RADIUS_M

        meters_per_deg = math.pi * EARTH_RADIUS_M / 180.0
        ranges = bbox_lon_ranges(35.68, 139.76, 1000)
        assert len(ranges) == 1
        lo, hi = ranges[0]
        center_half = 1000 / (meters_per_deg * math.cos(math.radians(35.68)))
        assert lo == pytest.approx(139.76 - center_half, rel=1e-4)
        assert hi == pytest.approx(139.76 + center_half, rel=1e-4)
        assert lo <= 139.76 - center_half
        assert hi >= 139.76 + center_half

    def test_bbox_lat_range_is_superset_of_haversine_reach(self):
        # A row due north at exactly the radius must fall inside the lat
        # window (prefilter ⊇ exact filter).
        from utils.geo_location import EARTH_RADIUS_M, bbox_lat_range

        radius = 1000.0
        exact_deg = math.degrees(radius / EARTH_RADIUS_M)
        lo, hi = bbox_lat_range(0.0, radius)
        assert hi >= exact_deg
        assert lo <= -exact_deg

    def test_bbox_antimeridian_split(self):
        # Near ±180° the window wraps: two ranges ORed together.
        ranges = bbox_lon_ranges(0.0, 179.999, 1000)
        assert len(ranges) == 2
        assert any(hi == LON_MAX for _, hi in ranges)
        assert any(lo == LON_MIN for lo, _ in ranges)

    def test_bbox_pole_fallback_covers_all_longitudes(self):
        # A window crossing a pole degenerates to the full longitude span.
        ranges = bbox_lon_ranges(89.9999, 0.0, 100_000)
        assert ranges == [(LON_MIN, LON_MAX)]

    def test_bbox_never_exceeds_lat_bounds(self):
        from utils.geo_location import bbox_lat_range

        lo, hi = bbox_lat_range(89.9999, 100_000)
        assert LAT_MIN <= lo <= hi <= LAT_MAX
