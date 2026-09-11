"""GET /api/v1/workers/active-color (#1482).

The marker reader is exercised against real files (tmp_path) because the
failure classes are filesystem facts — a directory where Docker expected a
file, a missing file, garbage content — and mocking ``open`` would only prove
the mock. The route is then driven through the real DI chain (``TestClient``
with the real ``verify_worker_token``) so the 401 is the dependency's, not
the test's.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes import workers as workers_mod
from services.active_color import read_active_color
from utils.exceptions import ActiveColorUnavailableError, MemoryCloudException

_TOKEN = "wt-secret"


def _settings(marker_path: str, deploy_color: str = "green", token: str = _TOKEN):
    return SimpleNamespace(
        worker_service_token=token,
        active_color_marker_path=marker_path,
        deploy_color=deploy_color,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(workers_mod.router, prefix="/api/v1")

    async def _mc_handler(_request, exc: MemoryCloudException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.error_code, "message": str(exc), **exc.details}},
        )

    app.add_exception_handler(MemoryCloudException, _mc_handler)
    return TestClient(app, raise_server_exceptions=False)


class TestReadActiveColor:
    def test_reads_the_marker_and_tolerates_the_trailing_newline(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        assert read_active_color(str(marker)) == "green"

    def test_case_and_surrounding_whitespace_are_normalized(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("  Blue \n")
        assert read_active_color(str(marker)) == "blue"

    def test_missing_marker_is_missing_not_blue(self, tmp_path):
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(tmp_path / "active-color"))
        assert exc.value.details["reason"] == "missing"
        assert exc.value.status_code == 503
        assert exc.value.error_code == "DEPLOY-001"

    def test_directory_in_place_of_the_marker_is_unreadable(self, tmp_path):
        # What Docker leaves behind when the host file did not exist at
        # `compose up`: a directory at the bind source.
        (tmp_path / "active-color").mkdir()
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(tmp_path / "active-color"))
        assert exc.value.details["reason"] == "unreadable"

    @pytest.mark.parametrize("content", ["", "\n", "purple\n", "blue green\n", "bl\x00ue"])
    def test_unknown_content_is_invalid_never_guessed(self, tmp_path, content):
        marker = tmp_path / "active-color"
        marker.write_bytes(content.encode())
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(marker))
        assert exc.value.details["reason"] == "invalid"

    def test_oversized_file_is_invalid_and_the_read_is_bounded(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_bytes(b"blue" + b" " * 4096 + b"\n")
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(marker))
        assert exc.value.details["reason"] == "invalid"

    def test_every_call_re_reads_the_file(self, tmp_path):
        # The whole point of the endpoint: a color switch must be visible on
        # the next request, with no restart and no cache.
        marker = tmp_path / "active-color"
        marker.write_text("blue\n")
        assert read_active_color(str(marker)) == "blue"
        marker.write_text("green\n")  # in-place, same inode, like deploy.sh
        assert read_active_color(str(marker)) == "green"


class TestActiveColorRoute:
    def test_normal_read_reports_marker_and_own_color(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        with patch.object(
            workers_mod, "get_settings", return_value=_settings(str(marker), "green")
        ):
            resp = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
        assert resp.status_code == 200
        assert resp.json() == {"active_color": "green", "responding_color": "green"}

    def test_mismatch_is_reported_not_hidden(self, tmp_path):
        # The drained color answered: the caller learns it reached the wrong
        # instance from the pair, which is the signal #1482 exists to expose.
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        with patch.object(workers_mod, "get_settings", return_value=_settings(str(marker), "blue")):
            resp = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
        assert resp.status_code == 200
        assert resp.json() == {"active_color": "green", "responding_color": "blue"}

    def test_uncolored_instance_reports_null_not_a_guess(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("blue\n")
        with patch.object(workers_mod, "get_settings", return_value=_settings(str(marker), "")):
            resp = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
        assert resp.status_code == 200
        assert resp.json() == {"active_color": "blue", "responding_color": None}

    def test_missing_marker_is_a_typed_503(self, tmp_path):
        with patch.object(
            workers_mod,
            "get_settings",
            return_value=_settings(str(tmp_path / "active-color"), "blue"),
        ):
            resp = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
        assert resp.status_code == 503
        body = resp.json()["error"]
        assert body["code"] == "DEPLOY-001"
        assert body["reason"] == "missing"
        assert "active_color" not in resp.json()

    def test_unauthenticated_is_401_before_the_marker_is_touched(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        with (
            patch.object(workers_mod, "get_settings", return_value=_settings(str(marker), "green")),
            patch.object(workers_mod, "read_active_color") as read,
        ):
            no_header = _client().get("/api/v1/workers/active-color")
            wrong = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": "Bearer nope"}
            )
        assert no_header.status_code == 401
        assert wrong.status_code == 401
        read.assert_not_called()
