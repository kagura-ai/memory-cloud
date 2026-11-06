"""The payload-free ingestion summary on real rows (#2679, AC 1).

Identity, receipt time, operation and processing status per event; never the
payload, document id or version; a time-sealed ascending cursor; expiry when
the cursor falls below the resource's oldest event.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio

from config.settings import get_settings
from models.resource import IndexerState, ResourceEvent
from services.observability_ingestion import (
    decode_ingestion_cursor,
    encode_ingestion_cursor,
    ingestion_doc_key,
    read_ingestion_page,
)
from services.observability_scope import resolve_scoped_resource
from tests.observability_helpers import World, app_client, bearer, issue_credential, world_rows
from utils.datetime import utcnow
from utils.hashing import hmac_sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "/api/v1/observability/ingestion-events"
SECRET_PAYLOAD = "zq-payload-marker-7731"
SECRET_DOC = "zq-docid-marker-4410"


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine):
    async with world_rows(async_engine) as w:
        yield w


@pytest.fixture(autouse=True)
def api_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "observability_api_enabled", True)


async def _events(w: World, key: str, specs: list[tuple[str, str, int]]) -> list[int]:
    """Insert ``(op, doc_id, age_seconds)`` events for resource ``key``; returns ids."""
    ids = []
    async with w.factory() as s:
        for n, (op, doc_id, age) in enumerate(specs):
            ev = ResourceEvent(
                resource_pk=w.resource_pk[key],
                resource_id=w.slugs[key],
                op=op,
                doc_id=doc_id,
                version=n + 1 if op == "upsert" else None,
                payload={"title": SECRET_PAYLOAD} if op == "upsert" else None,
                idempotency_key=f"idem-{SECRET_DOC}-{w.tag}-{key}-{n}-{age}",
                event_metadata={"source": SECRET_PAYLOAD},
                created_at=utcnow() - timedelta(seconds=age),
            )
            s.add(ev)
            await s.flush()
            ids.append(ev.id)
        await s.commit()
    return ids


async def _state(w: World, key: str, **fields) -> None:
    async with w.factory() as s:
        s.add(
            IndexerState(
                resource_pk=w.resource_pk[key],
                resource_id=w.slugs[key],
                context_id=w.ctx[key],
                **fields,
            )
        )
        await s.commit()


def test_doc_key_formula_matches_the_usage_events() -> None:
    pk = UUID("00000000-0000-4000-8000-000000000001")
    expected = hmac_sha256_hex(f"public-mcp-doc\n{pk}\nSKU-001", get_settings().audit_hmac_key)[:32]
    assert ingestion_doc_key(pk, "SKU-001") == expected
    assert len(expected) == 32
    assert ingestion_doc_key(UUID("00000000-0000-4000-8000-000000000002"), "SKU-001") != expected


async def test_page_shape_status_and_minimisation(world: World) -> None:
    ids = await _events(
        world,
        "a1",
        [
            ("upsert", f"{SECRET_DOC}-1", 3600),
            ("upsert", f"{SECRET_DOC}-2", 3500),
            ("delete", f"{SECRET_DOC}-1", 3400),
            ("upsert", f"{SECRET_DOC}-3", 5),  # younger than the seal
        ],
    )
    await _state(
        world,
        "a1",
        last_offset=ids[1],
        last_run_at=utcnow(),
        job_status="idle",
        metrics={"failed_events": [{"event_id": ids[0], "error": "boom-error-text"}]},
    )
    _, secret = await issue_credential(world)
    async with app_client(world) as c:
        r = await c.get(f"{BASE}?resource_id={world.slugs['a1']}", headers=bearer(secret))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["event_id"] for e in body["events"]] == [str(i) for i in ids[:3]]
    assert [e["op"] for e in body["events"]] == ["upsert", "upsert", "delete"]
    assert [e["processing"] for e in body["events"]] == [
        {"status": "processed", "failed": True},
        {"status": "processed", "failed": False},
        {"status": "pending", "failed": False},
    ]
    first = body["events"][0]
    assert first["doc_key"] == ingestion_doc_key(world.resource_pk["a1"], f"{SECRET_DOC}-1")
    assert body["events"][2]["doc_key"] == first["doc_key"]
    assert first["received_at"].endswith("Z")
    assert set(first) == {"event_id", "received_at", "op", "doc_key", "processing"}
    assert body["has_more"] is False
    assert decode_ingestion_cursor(body["next_cursor"]) == ids[2]
    assert body["coverage"]["seal_seconds"] == 120
    assert body["coverage"]["sealed_before"].endswith("Z")
    assert body["coverage"]["retention"] == "resource lifetime"
    assert body["coverage"]["oldest_event_id"] == str(ids[0])
    # Nothing from the payload, the document id, the version, the
    # idempotency key, the event metadata or the indexer's error text.
    for marker in (SECRET_PAYLOAD, SECRET_DOC, "boom-error-text", '"payload"', '"doc_id"'):
        assert marker not in r.text, marker
    assert not any("version" in key for e in body["events"] for key in e)


async def test_the_seal_and_cursor_chaining(world: World) -> None:
    ids = await _events(
        world, "a2", [("upsert", f"d{n}", 600 - n) for n in range(5)] + [("upsert", "young", 1)]
    )
    _, secret = await issue_credential(world, resource="a2")
    seen: list[str] = []
    cursor = None
    async with app_client(world) as c:
        for _ in range(5):
            url = f"{BASE}?limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = (await c.get(url, headers=bearer(secret))).json()
            seen += [e["event_id"] for e in body["events"]]
            cursor = body["next_cursor"]
            if not body["has_more"]:
                break
        # Replay of the last page answers the same rows; an empty page keeps the cursor.
        tail = (await c.get(f"{BASE}?cursor={cursor}", headers=bearer(secret))).json()
        assert tail["events"] == [] and tail["next_cursor"] == cursor
    assert seen == [str(i) for i in ids[:5]]
    # Without a state row the processing status is unknown.
    async with world.factory() as s:
        cred_scope = await _scope_for(world, s, secret)
        resource = await resolve_scoped_resource(s, cred_scope, None)
        page = await read_ingestion_page(s, resource, cursor=cursor, limit=10)
        assert page.events == []
        later = await read_ingestion_page(
            s, resource, cursor=cursor, limit=10, now=utcnow() + timedelta(seconds=200)
        )
        assert [e["event_id"] for e in later.events] == [str(ids[5])]
        assert later.events[0]["processing"] == {"status": "unknown", "failed": False}


async def _scope_for(w: World, s, secret: str):
    from auth.observation_credentials import ObservationCredentialManager

    scope = await ObservationCredentialManager(s).verify(secret)
    assert scope is not None
    return scope


async def test_cursor_expiry(world: World) -> None:
    from sqlalchemy import delete

    ids = await _events(world, "b1", [("upsert", f"p{n}", 900 - n) for n in range(6)])
    _, secret = await issue_credential(world, workspace="b")
    slug = world.slugs["b1"]
    async with app_client(world) as c:
        first = (await c.get(f"{BASE}?resource_id={slug}&limit=1", headers=bearer(secret))).json()
        consumed = first["next_cursor"]
        assert decode_ingestion_cursor(consumed) == ids[0]
        # A purge removes the next rows the client has not read yet.
        async with world.factory() as s:
            await s.execute(delete(ResourceEvent).where(ResourceEvent.id.in_(ids[:4])))
            await s.commit()
        r = await c.get(f"{BASE}?resource_id={slug}&cursor={consumed}", headers=bearer(secret))
        assert r.status_code == 410
        body = r.json()
        assert body["error"] == "cursor_expired"
        restart = body["details"]["oldest_cursor"]
        r = await c.get(f"{BASE}?resource_id={slug}&cursor={restart}", headers=bearer(secret))
        assert r.status_code == 200
        assert [e["event_id"] for e in r.json()["events"]] == [str(i) for i in ids[4:]]
        # The start cursor never expires.
        zero = encode_ingestion_cursor(0)
        r = await c.get(f"{BASE}?resource_id={slug}&cursor={zero}", headers=bearer(secret))
        assert r.status_code == 200
