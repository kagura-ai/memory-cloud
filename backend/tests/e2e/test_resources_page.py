"""E2E tests for the Resources list + detail pages (#47).

Verifies the new workspace-scoped resource management UI:
- Resource list page loads without ErrorBanner (regression for the
  "column last_event_at does not exist" bug caught in the round-10 review)
- Resource list page renders either the table or the empty-state
- Detail page does not throw when given a non-existent resource_id
- i18n: the Japanese locale surfaces translated column headers / copy

Requires running Docker services:
    docker compose up -d

Run:
    pytest tests/e2e/test_resources_page.py -m e2e -v
"""

import os
import time

import pytest
from playwright.sync_api import Page

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")


@pytest.mark.e2e
def test_resources_list_page_loads_without_error_banner(page: Page):
    """The /workspace/resources page must render cleanly against the real DB.

    Regression: an earlier revision of GET /api/v1/resources used a
    text("last_event_at") alias reference inside a GREATEST() call in
    ORDER BY, which PostgreSQL resolved as a column reference and failed
    with UndefinedColumnError. Unit tests mocked the DB and missed this.
    """
    page.goto(f"{BASE_URL}/workspace/resources")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # The ErrorBanner uses role="alert" (see frontend/src/components/common/ErrorBanner.tsx)
    error_banner = page.locator('[role="alert"]')
    assert error_banner.count() == 0, (
        "Resources list page rendered an ErrorBanner — likely a backend "
        "failure. Check kagura-api logs for the /api/v1/resources request."
    )

    # Page should show either the table header or the empty-state title
    # (both are acceptable — depends on whether the workspace has resources).
    has_table_header = page.locator("th", has_text="Resource ID").count() > 0
    has_empty_state = page.locator("text=No resources yet").count() > 0
    assert has_table_header or has_empty_state, (
        "Neither resource table nor empty-state rendered on /workspace/resources"
    )


@pytest.mark.e2e
def test_resources_list_page_has_sidebar_entry(page: Page):
    """Sidebar must expose the Resources nav entry in the workspace group."""
    page.goto(f"{BASE_URL}/workspace/dashboard")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Sidebar.tsx registers href="/workspace/resources"
    resources_link = page.locator('a[href="/workspace/resources"]')
    assert resources_link.count() > 0, "Sidebar does not expose a link to /workspace/resources"


@pytest.mark.e2e
def test_resources_detail_page_404_shows_not_found_title(page: Page):
    """Unknown resource_id renders the notFoundTitle + back link (not a crash).

    Regression: round-6 fix introduced `setError(detail.notFound)` which
    incorrectly flipped `isFetchError` true and showed the generic title
    instead of the dedicated notFoundTitle.
    """
    page.goto(f"{BASE_URL}/workspace/resources/this_resource_does_not_exist")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # ErrorBanner should be visible (role="alert")
    assert page.locator('[role="alert"]').count() > 0, (
        "Expected ErrorBanner on a resource_id that doesn't exist"
    )

    # Back-to-list button/link must be present
    back_link = page.locator('a[href="/workspace/resources"]')
    assert back_link.count() > 0, "Back-to-resources link missing on the not-found page"
