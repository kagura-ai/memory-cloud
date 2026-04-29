"""E2E test for JST display Layer 4 regression coverage (#491).

Verifies that the API serializes `last_activity_at` as a Z-suffixed UTC ISO 8601
string (per `TZAwareBaseModel` from #489) and that the frontend tooltip on
`/workspace/contexts` displays it in the user's `Profile.timezone`
(`Asia/Tokyo`) regardless of the browser's local timezone.

This is regression-only coverage for the "silent CI miss" class documented in
the JST debugging checklist:
  1. DB user.timezone column
  2. /api/v1/auth/me response
  3. Frontend code path (formatDateTime)
  4. **API datetime Z suffix** ← silent in default Vitest

Layer 4 cannot be caught by Vitest because Vitest defaults to UTC, so a naive
datetime (no `Z`, no offset) round-trips to the same display in tests but
shifts by the user's UTC offset in real non-UTC browsers. This Playwright
test forces the browser into a non-UTC zone (`Europe/London`) so the wire
format actually matters at parse time.

Requires running Docker services:
    docker compose up -d

Run:
    pytest tests/e2e/test_datetime_tz_display.py -m e2e -v
"""

import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
API_URL = os.environ.get("E2E_API_URL", "http://localhost:8080")
ADMIN_LOGIN_ID = os.environ.get("E2E_ADMIN_LOGIN_ID", "admin")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "adminPass123!!!")

# `Europe/London` is chosen because GMT/BST differ from both UTC (the
# baseline that masks the regression) and from Asia/Tokyo (the target
# Profile.timezone). When the test fails, the diagnostic is intuitive:
# "tooltip is shifted by ~9 hours, so JST conversion did not actually run."
NON_UTC_BROWSER_TZ = "Europe/London"
TARGET_PROFILE_TZ = "Asia/Tokyo"


@pytest.fixture(scope="module")
def jst_authenticated_context(browser):
    """Authenticated browser context with timezone forced to a non-UTC zone.

    Independent from `conftest.authenticated_context` (which uses the default
    browser TZ) because forcing a non-default browser timezone is essential
    to catching the Layer 4 regression — a UTC-only browser cannot
    distinguish naive from Z-suffixed wire formats. Only this test gets the
    override; the rest of the e2e suite keeps its default TZ.
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        timezone_id=NON_UTC_BROWSER_TZ,
    )
    page = context.new_page()

    page.goto(f"{BASE_URL}/login")
    page.wait_for_load_state("networkidle")

    admin_link = page.locator(
        "a, button", has_text=re.compile(r"管理者ログイン|Admin Login|管理者")
    )
    if admin_link.count() > 0:
        admin_link.first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1)

    page.fill('input[type="text"]', ADMIN_LOGIN_ID)
    page.fill('input[type="password"]', ADMIN_PASSWORD)

    terms_checkbox = page.locator('input[type="checkbox"], button[role="checkbox"]')
    if terms_checkbox.count() > 0:
        terms_checkbox.first.click()
        time.sleep(0.3)

    page.click('button[type="submit"]')
    page.wait_for_url("**/workspace/**", timeout=15000)
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

    Frontend uses `Intl.DateTimeFormat('ja-JP', { timeZone, year, month, day,
    hour, minute, second, hour12: false })`, which renders as
    `YYYY/MM/DD HH:mm:ss`. We replicate that exact shape here so the assertion
    is a literal string match.
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
    # Step 2: ensure the admin's Profile.timezone is the target JST value.
    profile_response = jst_page.request.put(
        f"{API_URL}/api/v1/users/profile",
        data={"timezone": TARGET_PROFILE_TZ},
    )
    assert profile_response.ok, (
        f"PUT /api/v1/users/profile failed: {profile_response.status} {profile_response.text()}"
    )

    # Step 3: read the canonical last_activity_at from the API.
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

    # Step 5: render the page and read the tooltip via `title` attribute.
    jst_page.goto(f"{BASE_URL}/workspace/contexts")
    jst_page.wait_for_load_state("networkidle")
    time.sleep(1)

    # The tooltip is rendered as `title=formatDateTime(last_activity_at, ...)`
    # on the span wrapping the relative-time text in the Last Activity column.
    # We anchor on the exact JST string — unique enough to disambiguate even
    # when the workspace has many contexts, since the formatted timestamp
    # (down to seconds) is effectively a row identifier.
    tooltip_locator = jst_page.locator(f'span[title="{expected_jst}"]')
    assert tooltip_locator.count() > 0, (
        f"No tooltip with the expected JST format was found on /workspace/contexts.\n"
        f"  API last_activity_at : {raw_last_activity!r}\n"
        f"  Expected JST tooltip : {expected_jst!r}\n"
        f"  Browser timezone     : {NON_UTC_BROWSER_TZ}\n"
        f"  Profile timezone     : {TARGET_PROFILE_TZ}\n"
        "Likely cause: Layer 4 regression. If the API returned a naive datetime, "
        "the browser parsed it as Europe/London local time and the tooltip is "
        "now shifted by the Europe/London ↔ Asia/Tokyo offset."
    )
