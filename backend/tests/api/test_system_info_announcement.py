"""``/system/info.announcement_consent_version`` (Issue #2761).

Null while ``ANNOUNCEMENT_EMAILS_ENABLED`` is off (the default): the sign-up
pages then show no opt-in checkbox and the login URLs stay as they were. Set to
the backend's consent wording version when on.
"""

from __future__ import annotations

from services.announcement_consent_service import ANNOUNCEMENT_CONSENT_WORDING_VERSION
from tests.api.test_system_info import info_with  # noqa: F401  (fixture)


def test_null_by_default(info_with) -> None:  # noqa: F811
    assert info_with().json()["announcement_consent_version"] is None


def test_the_wording_version_when_enabled(info_with) -> None:  # noqa: F811
    body = info_with(announcement_emails_enabled=True).json()

    assert body["announcement_consent_version"] == ANNOUNCEMENT_CONSENT_WORDING_VERSION
