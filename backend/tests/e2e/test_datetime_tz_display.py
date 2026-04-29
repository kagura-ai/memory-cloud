"""E2E test for JST display Layer 4 regression coverage (#491).

Verifies that the API serializes `last_activity_at` as a Z-suffixed UTC ISO 8601
string (per `TZAwareBaseModel` from #489) and that the frontend tooltip on
`/workspace/contexts` displays it in the user's `Profile.timezone`
(`Asia/Tokyo`) regardless of the browser's local timezone.

Layer 4 of the JST debugging checklist (API datetime Z-suffix) is silent under
default Vitest because Vitest runs in UTC; the regression only manifests in
non-UTC browsers. This test forces the browser into `Europe/London` so the
wire format actually matters at parse time.

Requires running Docker services:
    docker compose up -d

Run:
    pytest tests/e2e/test_datetime_tz_display.py -m e2e -v
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import API_URL, BASE_URL, login_admin

# `Europe/London` differs from both UTC (the baseline that masks the
# regression) and Asia/Tokyo (the target Profile.timezone) — when the test
# fails, the diagnostic is intuitive: tooltip is shifted by ~9 hours, so
# JST conversion did not actually run.
NON_UTC_BROWSER_TZ = "Europe/London"
TARGET_PROFILE_TZ = "Asia/Tokyo"


@pytest.fixture(scope="module")
def jst_authenticated_context(browser):
    """Authenticated context with timezone forced to a non-UTC zone.

    Independent from the session-scoped `authenticated_context` because the
    Layer 4 regression is invisible under a UTC browser; only this fixture
    overrides `timezone_id`, leaving the rest of the e2e suite unchanged.
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        timezone_id=NON_UTC_BROWSER_TZ,
    )
    page = context.new_page()
    login_admin(page)
    page.close()
    yield context
    context.close()


@pytest.fixture
def jst_page(jst_authenticated_context):
    """Per-test page in the timezone-forced authenticated context."""
    page = jst_authenticated_context.new_page()
    yield page
    page.close()


def _format_jst(utc_iso: str) -> str:
    """Convert a Z-suffixed UTC ISO 8601 string to the page's JST tooltip format.

    Mirrors the contract of `frontend/src/lib/utils/datetime.ts:26 formatDateTime()`
    invoked with `timezone='Asia/Tokyo'` and `locale='ja'` — the production call
    site at `frontend/src/app/(authenticated)/workspace/contexts/page.tsx:980-994`.
    Replicating the contract here (instead of importing a backend helper) keeps
    the assertion independent of backend formatting code.
    """
    dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    dt_jst = dt_utc.astimezone(ZoneInfo(TARGET_PROFILE_TZ))
    return dt_jst.strftime("%Y/%m/%d %H:%M:%S")


@pytest.mark.e2e
def test_last_activity_tooltip_displays_jst_under_non_utc_browser_tz(jst_page: Page):
    """Tooltip on /workspace/contexts shows JST consistently regardless of browser TZ.

    Issue #491 Layer 4 regression coverage. The test:

    1. Forces the browser timezone to `Europe/London` (non-UTC).
    2. Sets `Profile.timezone = Asia/Tokyo` via `PUT /api/v1/users/profile`.
    3. Reads `last_activity_at` from `GET /api/v1/contexts` and asserts the
       wire format ends with `Z` (the `TZAwareBaseModel` contract).
    4. Derives the expected JST display string from that UTC value.
    5. Visits `/workspace/contexts` and asserts the rendered tooltip matches.

    Failure modes this catches:
    - The API returns a naive datetime (no `Z`, no offset). The browser
      then parses it as `Europe/London` local time, and `Intl.DateTimeFormat`
      converts that wrong-local time to JST → tooltip is shifted by the
      Europe/London ↔ Asia/Tokyo offset.
    - `TZAwareBaseModel` is removed or bypassed → the wire-format assertion
      in step 3 fails before the UI assertion runs, with a precise diagnostic.

    Skips when the admin workspace has no context with `last_activity_at` set
    (a test-data condition, not a regression).
    """
    profile_response = jst_page.request.put(
        f"{API_URL}/api/v1/users/profile",
        data={"timezone": TARGET_PROFILE_TZ},
    )
    assert profile_response.ok, (
        f"PUT /api/v1/users/profile failed: {profile_response.status} {profile_response.text()}"
    )

    contexts_response = jst_page.request.get(f"{API_URL}/api/v1/contexts")
    assert contexts_response.ok, f"GET /api/v1/contexts failed: {contexts_response.status}"
    payload = contexts_response.json()
    contexts = payload.get("contexts") if isinstance(payload, dict) else payload
    assert isinstance(contexts, list), (
        f"Unexpected /api/v1/contexts shape: {type(payload).__name__}"
    )

    contexts_with_activity = [c for c in contexts if c.get("last_activity_at")]
    if not contexts_with_activity:
        pytest.skip(
            "No context in the admin workspace has last_activity_at set — "
            "regression coverage requires at least one active context. "
            "Add a memory to any context to populate last_activity_at."
        )

    target = contexts_with_activity[0]
    raw_last_activity = target["last_activity_at"]

    # Layer 4 wire-format contract: TZAwareBaseModel guarantees a `Z` suffix
    # on UTC datetimes. A bare ISO 8601 with no offset would be the silent-
    # regression smoking gun.
    assert raw_last_activity.endswith("Z"), (
        f"last_activity_at wire format is not Z-suffixed: {raw_last_activity!r}. "
        "TZAwareBaseModel may have been removed or bypassed for this response "
        "schema (regression of #489)."
    )

    expected_jst = _format_jst(raw_last_activity)

    jst_page.goto(f"{BASE_URL}/workspace/contexts")
    jst_page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Anchor on the exact JST string — second-precision makes it row-unique.
    tooltip_locator = jst_page.locator(f'span[title="{expected_jst}"]')
    assert tooltip_locator.count() > 0, (
        f"No tooltip with the expected JST format was found on /workspace/contexts.\n"
        f"  API last_activity_at : {raw_last_activity!r}\n"
        f"  Expected JST tooltip : {expected_jst!r}\n"
        f"  Browser timezone     : {NON_UTC_BROWSER_TZ}\n"
        f"  Profile timezone     : {TARGET_PROFILE_TZ}"
    )
