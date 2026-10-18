"""The singleton Qdrant client asks for uncompressed REST responses (#2112).

A compressed Qdrant response is streamed with ``Transfer-Encoding: chunked``.
At some response sizes its last data segment is small, the client delays the
ACK for it, and the server's Nagle holds the 5-byte chunk terminator until
that ACK arrives: a ~40 ms stall. An identity response carries
``Content-Length`` and has no terminator. These tests drive the real
``AsyncQdrantClient`` that ``get_qdrant_client`` builds through an httpx mock
transport (no Qdrant is contacted; the compatibility probe is off), check the
header on the wire, and check that an identity response and a compressed one
give the same search results.
"""

import gzip
import json
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


def _query_response(request: httpx.Request) -> httpx.Response:
    """Qdrant's query answer, gzip-compressed when the request allows it."""
    points = [{"version": 1, "payload": {"summary": f"s{i}"}, **p} for i, p in enumerate(POINTS)]
    body = json.dumps({"result": {"points": points}, "status": "ok", "time": 0.001}).encode()
    headers = {"content-type": "application/json"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body)
        headers["content-encoding"] = "gzip"
    return httpx.Response(200, content=body, headers=headers)


@pytest.fixture
async def wire(monkeypatch):
    """Yield ``build(compression)`` -> fresh singleton over a mock transport, and the log."""
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    built: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        responses.append(_query_response(request))
        return responses[-1]

    real_client = qmod.AsyncQdrantClient

    def client_over_mock(**kwargs):
        client = real_client(
            **kwargs, check_compatibility=False, transport=httpx.MockTransport(handler)
        )
        built.append(client)
        return client

    import config.settings as settings_mod

    def build(compression: bool):
        monkeypatch.setattr(
            settings_mod,
            "get_settings",
            lambda: SimpleNamespace(qdrant_api_key=None, qdrant_http_compression=compression),
        )
        qmod._qdrant_client = None
        return get_qdrant_client()

    original = qmod._qdrant_client
    monkeypatch.setattr(qmod, "AsyncQdrantClient", client_over_mock)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    yield SimpleNamespace(build=build, requests=requests, responses=responses)
    qmod._qdrant_client = original
    for client in built:
        await client.close()


async def _search() -> list[tuple[str, float, str]]:
    results = await search_memories_qdrant(
        UID, [0.1, 0.2, 0.3], WS, CTX, limit=90, include_chunks=True
    )
    return [(str(r["id"]), r["score"], r["payload"]["summary"]) for r in results]


async def test_rest_request_asks_for_identity_encoding(wire):
    client = wire.build(compression=False)
    assert get_qdrant_client() is client

    await _search()

    assert len(wire.requests) == 1
    assert wire.requests[0].url.path.endswith("/points/query")
    assert wire.requests[0].headers["accept-encoding"] == "identity"
    assert "content-encoding" not in wire.responses[0].headers


async def test_identity_and_compressed_responses_give_the_same_results(wire):
    wire.build(compression=False)
    identity = await _search()
    wire.build(compression=True)
    compressed = await _search()

    # The opt-in client keeps httpx's default encodings; the mock compressed it.
    assert "gzip" in wire.requests[1].headers["accept-encoding"]
    assert wire.responses[1].headers["content-encoding"] == "gzip"
    expected = [(p["id"], p["score"], f"s{i}") for i, p in enumerate(POINTS)]
    assert identity == compressed == expected
