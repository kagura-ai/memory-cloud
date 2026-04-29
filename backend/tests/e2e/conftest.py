"""Playwright E2E test fixtures.

Provides authenticated browser page for frontend E2E tests.
Requires running Docker services (frontend + API).

Usage:
    pytest tests/e2e/test_search_settings.py -m e2e
"""

import os
import re

import pytest
from playwright.sync_api import Page, sync_playwright

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
API_URL = os.environ.get("E2E_API_URL", "http://localhost:8080")
ADMIN_LOGIN_ID = os.environ.get("E2E_ADMIN_LOGIN_ID", "admin")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "adminPass123!!!")


@pytest.fixture(scope="session")
def browser():
    """Launch browser for the test session."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def login_admin(page: Page) -> None:
    """Perform the admin login flow on the given page.

    Shared by every authenticated_context fixture variant so login UI
    changes (selectors, terms checkbox, redirect target) only need updating
    here. Terminates on the post-login `**/workspace/**` redirect; callers
    own the page lifecycle around it.
    """
    page.goto(f"{BASE_URL}/login")
    page.wait_for_load_state("networkidle")

    admin_link = page.locator(
        "a, button", has_text=re.compile(r"管理者ログイン|Admin Login|管理者")
    )
    if admin_link.count() > 0:
        admin_link.first.click()
        page.wait_for_load_state("networkidle")

    # Wait deterministically for the login form input to be present —
    # replaces the previous time.sleep(1) hard-pause after the admin-link
    # click (which only fires on first login but is shared across every
    # authenticated_context variant).
    page.wait_for_selector('input[type="text"]', state="visible", timeout=10000)
    page.fill('input[type="text"]', ADMIN_LOGIN_ID)
    page.fill('input[type="password"]', ADMIN_PASSWORD)

    terms_checkbox = page.locator('input[type="checkbox"], button[role="checkbox"]')
    if terms_checkbox.count() > 0:
        terms_checkbox.first.click()
        # No explicit wait — the next click('button[type="submit"]') auto-waits
        # for the submit button to be enabled (Playwright actionability check),
        # which implicitly waits for the checkbox click to flip the form into
        # a submittable state.

    page.click('button[type="submit"]')
    page.wait_for_url("**/workspace/**", timeout=15000)


@pytest.fixture(scope="session")
def authenticated_context(browser):
    """Create an authenticated browser context (login once per session)."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    login_admin(page)
    page.close()
    yield context
    context.close()


@pytest.fixture
def page(authenticated_context) -> Page:
    """Create a new page in the authenticated context."""
    page = authenticated_context.new_page()
    yield page
    page.close()
