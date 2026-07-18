"""WHERE-axis geo filter on the Qdrant search legs (#1332).

``filters["near"] = {lat, lon, radius_m}`` becomes a ``geo_radius`` payload
condition on the ``location`` field inside ``_build_search_filter`` — the
single hook point shared by the semantic and BM25 legs, so there is no
per-leg drift (#1229 lesson). Collection bootstrap gains a ``geo`` payload
index with a retrofit branch for existing collections (tags precedent).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.qdrant as qmod
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_COLLECTION,
    _build_search_filter,
    ensure_kagura_memories_collection,
)
from utils.geo_location import DEFAULT_RADIUS_M, LocationValidationError

WS = "ws-1"
CTX = "ctx-1"
UID = "google-oauth2|user-123"


def _location_conditions(qfilter):
    return [c for c in qfilter.must if getattr(c, "key", None) == "location"]


class TestNearFilterCondition:
    def test_near_adds_geo_radius_condition(self):
        qfilter = _build_search_filter(
            WS, CTX, UID, filters={"near": {"lat": 35.6812, "lon": 139.7671, "radius_m": 500}}
        )
        conds = _location_conditions(qfilter)
        assert len(conds) == 1
        geo = conds[0].geo_radius
        assert geo.center.lat == pytest.approx(35.6812)
        assert geo.center.lon == pytest.approx(139.7671)
        assert geo.radius == pytest.approx(500.0)

    def test_near_radius_defaults(self):
        qfilter = _build_search_filter(WS, CTX, UID, filters={"near": {"lat": 0.0, "lon": 0.0}})
        assert _location_conditions(qfilter)[0].geo_radius.radius == pytest.approx(DEFAULT_RADIUS_M)

    def test_no_near_no_location_condition(self):
        qfilter = _build_search_filter(WS, CTX, UID, filters={"type": "note"})
        assert _location_conditions(qfilter) == []

    def test_malformed_near_fails_closed(self):
        # #1229: a present-but-broken filter must raise (→ 4xx), never be
        # silently dropped into an unfiltered search.
        with pytest.raises(LocationValidationError):
            _build_search_filter(WS, CTX, UID, filters={"near": {"lat": 95.0, "lon": 0.0}})

    def test_near_composes_with_other_filters(self):
        qfilter = _build_search_filter(
            WS,
            CTX,
            UID,
            filters={"type": "note", "near": {"lat": 1.0, "lon": 2.0, "radius_m": 100}},
        )
        keys = [getattr(c, "key", None) for c in qfilter.must]
        assert "type" in keys
        assert "location" in keys


class TestEnsureCollectionGeoIndex:
    @pytest.fixture
    def mock_client(self, monkeypatch):
        client = AsyncMock()
        monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
        return client

    def _geo_index_calls(self, client):
        return [
            c
            for c in client.create_payload_index.await_args_list
            if c.kwargs.get("field_name") == "location"
        ]

    @pytest.mark.asyncio
    async def test_create_branch_includes_geo_index(self, mock_client):
        mock_client.get_collections.return_value = SimpleNamespace(collections=[])
        await ensure_kagura_memories_collection(512)
        calls = self._geo_index_calls(mock_client)
        assert len(calls) == 1
        assert calls[0].kwargs["field_schema"] == "geo"

    @pytest.mark.asyncio
    async def test_retrofit_adds_missing_geo_index(self, mock_client):
        # tags precedent: an up-to-date (sparse-enabled) collection lacking
        # the location index gets it created on ensure.
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=params),
            payload_schema={"tags": object(), "scope": object()},
        )
        await ensure_kagura_memories_collection()
        calls = self._geo_index_calls(mock_client)
        assert len(calls) == 1
        assert calls[0].kwargs["field_schema"] == "geo"
        mock_client.create_collection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retrofit_skips_when_geo_index_present(self, mock_client):
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION)]
        )
        params = SimpleNamespace(sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()})
        mock_client.get_collection.return_value = SimpleNamespace(
            config=SimpleNamespace(params=params),
            payload_schema={"tags": object(), "location": object()},
        )
        await ensure_kagura_memories_collection()
        mock_client.create_payload_index.assert_not_awaited()


class TestWithinFilterCondition:
    def test_within_adds_geo_polygon_condition(self):
        qfilter = _build_search_filter(
            WS,
            CTX,
            UID,
            filters={
                "within": {
                    "polygon": [
                        {"lat": 0.0, "lon": 0.0},
                        {"lat": 0.0, "lon": 1.0},
                        {"lat": 1.0, "lon": 1.0},
                    ]
                }
            },
        )
        conds = _location_conditions(qfilter)
        assert len(conds) == 1
        exterior = conds[0].geo_polygon.exterior
        # Auto-closed ring: first == last.
        assert exterior.points[0].lat == exterior.points[-1].lat
        assert exterior.points[0].lon == exterior.points[-1].lon
        assert len(exterior.points) == 4

    def test_malformed_within_fails_closed(self):
        with pytest.raises(LocationValidationError):
            _build_search_filter(
                WS, CTX, UID, filters={"within": {"polygon": [{"lat": 0.0, "lon": 0.0}]}}
            )

    def test_near_and_within_compose(self):
        qfilter = _build_search_filter(
            WS,
            CTX,
            UID,
            filters={
                "near": {"lat": 0.5, "lon": 0.5, "radius_m": 1000},
                "within": {
                    "polygon": [
                        {"lat": 0.0, "lon": 0.0},
                        {"lat": 0.0, "lon": 1.0},
                        {"lat": 1.0, "lon": 1.0},
                    ]
                },
            },
        )
        conds = _location_conditions(qfilter)
        assert len(conds) == 2
