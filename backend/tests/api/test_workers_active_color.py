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
from fastapi.testclient import TestClient

from api.main import memory_cloud_exception_handler
from api.routes import workers as workers_mod
from services.active_color import read_active_color, read_active_color_settled
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
    # The REAL handler, so the 503 body asserted below is the envelope
    # production emits ({"error": code, "message", "details": {...}}), not a
    # shape invented here.
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_last_color_pair():
    workers_mod._last_color_pair = None
    yield
    workers_mod._last_color_pair = None


class TestReadActiveColor:
    def test_reads_the_marker_and_tolerates_the_trailing_newline(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        assert read_active_color(str(marker)) == "green"

    @pytest.mark.parametrize("content", ["Blue\n", " blue\n", "blue \n", "GREEN"])
    def test_case_and_padding_are_refused_like_deploy_sh_does(self, tmp_path, content):
        # deploy.sh get_active_color compares the exact bytes; a marker the
        # writer's own validator rejects must not be accepted here, or the
        # endpoint and the next deploy disagree about the same file.
        marker = tmp_path / "active-color"
        marker.write_text(content)
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(marker))
        assert exc.value.details["reason"] == "invalid"

    def test_crlf_trailing_newline_is_tolerated(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_bytes(b"green\r\n")
        assert read_active_color(str(marker)) == "green"

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

    def test_empty_read_is_flagged_so_the_caller_can_step_over_the_cp_window(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_bytes(b"")
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(marker))
        assert exc.value.empty is True
        marker.write_bytes(b"purple\n")
        with pytest.raises(ActiveColorUnavailableError) as exc:
            read_active_color(str(marker))
        assert exc.value.empty is False

    @pytest.mark.asyncio
    async def test_settled_read_retries_once_after_an_empty_read(self, tmp_path):
        # The in-place republish: cp truncates, then writes. A read in between
        # sees zero bytes and must not be reported as a corrupt marker.
        marker = tmp_path / "active-color"
        marker.write_bytes(b"")
        with patch("services.active_color.read_active_color") as read:
            read.side_effect = [
                ActiveColorUnavailableError(reason="invalid", path=str(marker), empty=True),
                "green",
            ]
            assert await read_active_color_settled(str(marker)) == "green"
        assert read.call_count == 2

    @pytest.mark.asyncio
    async def test_settled_read_does_not_retry_a_non_empty_failure(self, tmp_path):
        marker = tmp_path / "active-color"
        with patch("services.active_color.read_active_color") as read:
            read.side_effect = ActiveColorUnavailableError(reason="missing", path=str(marker))
            with pytest.raises(ActiveColorUnavailableError):
                await read_active_color_settled(str(marker))
        assert read.call_count == 1

    @pytest.mark.asyncio
    async def test_settled_read_still_fails_on_a_genuinely_empty_marker(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_bytes(b"")
        with pytest.raises(ActiveColorUnavailableError) as exc:
            await read_active_color_settled(str(marker))
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
        body = resp.json()
        assert body["error"] == "DEPLOY-001"
        assert body["details"]["reason"] == "missing"
        assert body["details"]["marker_path"] == str(tmp_path / "active-color")
        assert "active_color" not in body

    def test_mismatch_is_logged_once_per_transition_not_per_request(self, tmp_path):
        # A poller on a drained color (which stays up until the next deploy
        # after a rollback) must not produce one WARN per poll; the body
        # carries the signal every time, the log only when the pair changes.
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        with (
            patch.object(workers_mod, "get_settings", return_value=_settings(str(marker), "blue")),
            patch.object(workers_mod.logger, "warning") as warn,
        ):
            client = _client()
            for _ in range(3):
                assert (
                    client.get(
                        "/api/v1/workers/active-color",
                        headers={"Authorization": f"Bearer {_TOKEN}"},
                    ).status_code
                    == 200
                )
            assert warn.call_count == 1
            # The marker flips back to blue: pair changes, but it now matches.
            marker.write_text("blue\n")
            client.get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
            assert warn.call_count == 1
            # ...and drifts again: a NEW transition, logged once more.
            marker.write_text("green\n")
            client.get(
                "/api/v1/workers/active-color", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
            assert warn.call_count == 2

    def test_unauthenticated_is_401_before_the_marker_is_touched(self, tmp_path):
        marker = tmp_path / "active-color"
        marker.write_text("green\n")
        with (
            patch.object(workers_mod, "get_settings", return_value=_settings(str(marker), "green")),
            patch.object(workers_mod, "read_active_color_settled") as read,
        ):
            no_header = _client().get("/api/v1/workers/active-color")
            wrong = _client().get(
                "/api/v1/workers/active-color", headers={"Authorization": "Bearer nope"}
            )
        assert no_header.status_code == 401
        assert wrong.status_code == 401
        read.assert_not_called()
