"""The singleton Qdrant client asks for uncompressed REST responses (#2112).

A compressed Qdrant response is streamed with ``Transfer-Encoding: chunked``.
At some response sizes its last data segment is small, the client delays the
ACK for it, and the server's Nagle holds the 5-byte chunk terminator until
that ACK arrives: a ~40 ms stall. An identity response carries
``Content-Length`` and has no terminator. These tests drive the real
``AsyncQdrantClient`` that ``get_qdrant_client`` builds through an httpx mock
transport (no Qdrant is contacted), check the header on the wire, and check
that search results pass through unchanged.
"""

from types import SimpleNamespace

import httpx
import pytest

import db.qdrant as qmod
from db.qdrant import get_qdrant_client, search_memories_qdrant

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
UID = "google-oauth2|user-123"

# Canned dense hits, deliberately not in id order: the scores fix the order.
POINTS = [
    {"id": "33333333-3333-4333-8333-333333333333", "score": 0.91},
    {"id": "55555555-5555-4555-8555-555555555555", "score": 0.77},
    {"id": "44444444-4444-4444-8444-444444444444", "score": 0.64},
]


@pytest.fixture
def wire(monkeypatch):
    """Build the singleton over a mock transport; yield the recorded requests."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        points = [
            {"version": 1, "payload": {"summary": f"s{i}"}, **p} for i, p in enumerate(POINTS)
        ]
        return httpx.Response(
            200, json={"result": {"points": points}, "status": "ok", "time": 0.001}
        )

    real_client = qmod.AsyncQdrantClient

    def client_over_mock(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    import config.settings as settings_mod

    original = qmod._qdrant_client
    qmod._qdrant_client = None
    monkeypatch.setattr(qmod, "AsyncQdrantClient", client_over_mock)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    monkeypatch.setattr(settings_mod, "get_settings", lambda: SimpleNamespace(qdrant_api_key=None))
    yield requests
    qmod._qdrant_client = original


async def test_rest_request_asks_for_identity_encoding(wire):
    await search_memories_qdrant(UID, [0.1, 0.2, 0.3], WS, CTX, limit=3)

    assert len(wire) == 1
    assert wire[0].url.path.endswith("/points/query")
    assert wire[0].headers["accept-encoding"] == "identity"


async def test_search_results_keep_ids_scores_and_order(wire):
    results = await search_memories_qdrant(
        UID, [0.1, 0.2, 0.3], WS, CTX, limit=90, include_chunks=True
    )

    assert [(str(r["id"]), r["score"]) for r in results] == [(p["id"], p["score"]) for p in POINTS]
    assert [r["payload"]["summary"] for r in results] == ["s0", "s1", "s2"]


async def test_singleton_is_reused(wire):
    assert get_qdrant_client() is get_qdrant_client()
