"""#2043: ``PATCH /api/v1/memory/{id}`` with a stale ``expected_version`` is a 409.

The house body of a ``ConflictError`` (``RES-002``) with the ``reason``
discriminator and the current version, so a client can re-read and resend.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from utils.exceptions import VersionConflictError


def test_rest_patch_answers_409_with_the_current_version() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.main import memory_cloud_exception_handler
    from api.routes.memory import get_memory_service, router
    from auth.dependencies import get_user_from_api_key_or_session
    from utils.exceptions import MemoryCloudException

    memory_id = uuid4()
    service = MagicMock()
    service.patch_memory = AsyncMock(
        side_effect=VersionConflictError(memory_id=memory_id, expected_version=1, current_version=2)
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/memory")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: {"user_id": "u1"}
    app.dependency_overrides[get_memory_service] = lambda: service

    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/memory/{memory_id}", json={"importance": 0.9, "expected_version": 1}
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "RES-002"
    assert body["details"] == {
        "reason": "version_conflict",
        "expected_version": 1,
        "current_version": 2,
    }
    assert service.patch_memory.await_args.kwargs["request"].expected_version == 1
