"""The worker ``/schema`` answer of a real installation, byte for byte (#3055).

``tests/fixtures/sync-schema-v1.json`` holds the exact bytes of the answer of
a version-1 installation: a consumer may copy it as a strict object, so no key,
value or separator may move. Not executed in the local unit run: needs the DB
container.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import service

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds

FIXTURES = Path(__file__).parents[1] / "fixtures"
V1_SHA256 = "936fd4e0746941f8db3500cb5a91b6252a77b559d9bd48cb41eb36a6f95c6f32"


async def test_a_version_one_installation_reads_the_exact_previous_answer(feeds):
    response = await service(feeds, "GET", "schema")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    expected = (FIXTURES / "sync-schema-v1.json").read_bytes().removesuffix(b"\n")
    assert response.content == expected
    assert len(response.content) == 1864
    assert hashlib.sha256(response.content).hexdigest() == V1_SHA256
