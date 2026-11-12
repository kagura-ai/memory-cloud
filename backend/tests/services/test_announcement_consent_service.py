"""Announcement-email consent: the pure parts (Issue #2761).

The DB behaviour (append-only history, newest row wins, recipient selection,
the users-row lock) is covered on Postgres in
``tests/integration/test_announcement_consent_db.py``. Pinned here:

- the wording version is offered only while ``ANNOUNCEMENT_EMAILS_ENABLED``
  is on;
- ``resolve_signup_consent`` is the one validator every sign-up path uses: it
  never raises and returns ``None`` unless the feature is on, the value is a
  real bool and the version is the current one;
- ``new_consent_row`` is a pure builder that refuses a "yes" without a
  wording version;
- the recipient helper selects nobody while the feature is off, without a
  query.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import get_settings
from models.announcement_consent import ANNOUNCEMENT_CONSENT_SOURCES, AnnouncementEmailConsent
from services import announcement_consent_service as svc


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)


@pytest.fixture
def disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)


CURRENT = svc.ANNOUNCEMENT_CONSENT_WORDING_VERSION


class TestWordingVersion:
    def test_off_by_default(self) -> None:
        from config.settings import Settings

        assert Settings(_env_file=None).announcement_emails_enabled is False

    def test_none_while_disabled(self, disabled) -> None:
        assert svc.announcement_emails_enabled() is False
        assert svc.current_consent_wording_version() is None

    def test_the_constant_while_enabled(self, enabled) -> None:
        assert svc.announcement_emails_enabled() is True
        assert svc.current_consent_wording_version() == CURRENT

    def test_the_version_has_the_url_safe_shape(self) -> None:
        from config.settings import TERMS_VERSION_RE

        assert TERMS_VERSION_RE.fullmatch(CURRENT)


class TestResolveSignupConsent:
    @pytest.mark.parametrize("value", [True, False])
    def test_current_version_is_kept(self, enabled, value: bool) -> None:
        consent = svc.resolve_signup_consent(value, CURRENT)

        assert consent == svc.SignupAnnouncementConsent(value=value, wording_version=CURRENT)

    def test_disabled_drops_it(self, disabled) -> None:
        assert svc.resolve_signup_consent(True, CURRENT) is None
        assert svc.resolve_signup_consent(False, CURRENT) is None

    @pytest.mark.parametrize("version", [None, "", "2020-01", CURRENT + "x", 5, b"2026-11"])
    def test_a_stale_or_malformed_version_drops_it(self, enabled, version) -> None:
        assert svc.resolve_signup_consent(True, version) is None

    @pytest.mark.parametrize("value", [None, "true", "false", 1, 0, "on"])
    def test_a_value_that_is_not_a_bool_drops_it(self, enabled, value) -> None:
        assert svc.resolve_signup_consent(value, CURRENT) is None


class TestNewConsentRow:
    def test_builds_an_unsaved_row(self) -> None:
        row = svc.new_consent_row(
            user_id="u1", value=True, source="signup", wording_version=CURRENT
        )

        assert isinstance(row, AnnouncementEmailConsent)
        assert (row.user_id, row.value, row.source, row.wording_version) == (
            "u1",
            True,
            "signup",
            CURRENT,
        )
        assert row.id is None

    def test_a_withdrawal_needs_no_version(self) -> None:
        row = svc.new_consent_row(
            user_id="u1", value=False, source="unsubscribe_link", wording_version=None
        )

        assert row.value is False
        assert row.wording_version is None

    def test_a_yes_without_a_version_is_refused(self) -> None:
        with pytest.raises(ValueError):
            svc.new_consent_row(user_id="u1", value=True, source="settings", wording_version=None)

    def test_an_unknown_source_is_refused(self) -> None:
        with pytest.raises(ValueError):
            svc.new_consent_row(
                user_id="u1",
                value=False,
                source="api",  # type: ignore[arg-type]
                wording_version=None,
            )

    def test_sources_match_the_model(self) -> None:
        assert ANNOUNCEMENT_CONSENT_SOURCES == ("signup", "settings", "unsubscribe_link")


class TestRecipientsWhileDisabled:
    @pytest.mark.asyncio
    async def test_nobody_and_no_query(self, disabled) -> None:
        session = AsyncMock()

        assert await svc.select_announcement_recipients(session) == []
        session.execute.assert_not_awaited()


class TestRecordValidation:
    @pytest.mark.asyncio
    async def test_a_yes_without_a_version_is_refused_before_any_query(self) -> None:
        session = AsyncMock()

        with pytest.raises(ValueError):
            await svc.record_announcement_consent(
                session, user_id="u1", value=True, source="settings", wording_version=None
            )
        session.execute.assert_not_awaited()
        session.scalar.assert_not_awaited()
