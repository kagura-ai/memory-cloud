"""#1756: the UI's resource-ingest curl samples must be requests the API accepts.

Both copy-paste samples (the connector-created dialog and the Resource tokens
tab guide) build their ``-d`` body from
``frontend/src/lib/connectors/resourceIngestSampleBodies.json``. The frontend
test ``resourceIngestSample.test.ts`` pins the rendered curl text to that
fixture; this test validates every fixture body against the real
``ResourceEventRequest``, so a sample missing a required field (the v0.82.0
connector sample had no ``version``) fails here instead of in a user's terminal.
"""

import json
from pathlib import Path

import pytest

from models.schemas import ResourceEventRequest

# backend/tests/models/<this file> -> models -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "frontend/src/lib/connectors/resourceIngestSampleBodies.json"

_SAMPLES = ("connectorTest", "resourceTokenGuide")


def _bodies() -> dict[str, dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_exactly_the_known_samples() -> None:
    # A new sample must be added to ``_SAMPLES`` so it gets validated below.
    assert set(_bodies()) == set(_SAMPLES)


@pytest.mark.parametrize("name", _SAMPLES)
def test_sample_body_is_accepted(name: str) -> None:
    body = _bodies()[name]
    req = ResourceEventRequest.model_validate(body)
    assert req.op == "upsert"
    assert req.version is not None and req.version >= 1
    assert req.payload
