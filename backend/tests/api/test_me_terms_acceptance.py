"""``POST /api/v1/me/terms-acceptance`` and ``system/info.terms_version`` (Issue #1665).

- disabled (``TERMS_VERSION`` empty) → 404, nothing recorded; ``terms_version``
  is ``null`` on ``/system/info``;
- a version other than the current one → 409 with a clear detail;
- the current version → 200, recorded as ``reaccept``; a repeat is still 200;
- session auth is required (API keys are refused like on every ``/me`` route).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from api.routes import me_terms
from config.settings import Settings, get_settings
from services.terms_service import RecordResult

VERSION = "2026-09"
USER = {"user_id": "u1", "email": "u@example.test"}


def _request() -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"), headers={"user-agent": "t"})


@pytest.fixture
def record(monkeypatch) -> AsyncMock:
    stub = AsyncMock(return_value=RecordResult(version=VERSION, recorded=True))
    monkeypatch.setattr(me_terms, "TermsService", MagicMock(return_value=MagicMock(record=stub)))
    return stub


class TestAcceptTerms:
    @pytest.mark.asyncio
    async def test_disabled_is_404(self, monkeypatch, record) -> None:
        monkeypatch.setattr(get_settings(), "terms_version", "")

        with pytest.raises(HTTPException) as exc:
            await me_terms.accept_terms(
                me_terms.TermsAcceptanceRequest(version=VERSION), _request(), USER, db=MagicMock()
            )

        assert exc.value.status_code == 404
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mismatch_is_409(self, monkeypatch, record) -> None:
        monkeypatch.setattr(get_settings(), "terms_version", VERSION)

        with pytest.raises(HTTPException) as exc:
            await me_terms.accept_terms(
                me_terms.TermsAcceptanceRequest(version="2025-01"),
                _request(),
                USER,
                db=MagicMock(),
            )

        assert exc.value.status_code == 409
        assert "terms have changed" in exc.value.detail
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_match_records_reaccept(self, monkeypatch, record) -> None:
        monkeypatch.setattr(get_settings(), "terms_version", VERSION)

        response = await me_terms.accept_terms(
            me_terms.TermsAcceptanceRequest(version=VERSION), _request(), USER, db=MagicMock()
        )

        assert response.version == VERSION
        assert response.recorded is True
        assert response.terms_acceptance_required is False
        kwargs = record.await_args.kwargs
        assert kwargs["user_id"] == "u1"
        assert kwargs["user_email"] == "u@example.test"
        assert kwargs["version"] == VERSION
        assert kwargs["source"] == "reaccept"
        assert kwargs["ip_address"] == "203.0.113.7"

    @pytest.mark.asyncio
    async def test_repeat_is_still_ok(self, monkeypatch, record) -> None:
        monkeypatch.setattr(get_settings(), "terms_version", VERSION)
        record.return_value = RecordResult(version=VERSION, recorded=False)

        response = await me_terms.accept_terms(
            me_terms.TermsAcceptanceRequest(version=VERSION), _request(), USER, db=MagicMock()
        )

        assert response.recorded is False
        assert response.terms_acceptance_required is False

    def test_requires_a_session(self) -> None:
        resp = TestClient(app).post("/api/v1/me/terms-acceptance", json={"version": VERSION})
        assert resp.status_code == 401

    def test_body_is_bounded(self) -> None:
        with pytest.raises(ValueError):
            me_terms.TermsAcceptanceRequest(version="x" * 65)
        with pytest.raises(ValueError):
            me_terms.TermsAcceptanceRequest(version="")


class TestSystemInfo:
    def _info(self, monkeypatch, **overrides) -> dict:
        monkeypatch.delenv("TERMS_VERSION", raising=False)
        settings = Settings(_env_file=None, **overrides)
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)
        resp = TestClient(app).get("/api/v1/system/info")
        assert resp.status_code == 200
        return resp.json()

    def test_null_when_disabled(self, monkeypatch) -> None:
        assert self._info(monkeypatch)["terms_version"] is None

    def test_exposes_the_configured_version(self, monkeypatch) -> None:
        assert self._info(monkeypatch, terms_version=VERSION)["terms_version"] == VERSION
