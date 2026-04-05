"""Playwright E2E test fixtures.

Provides authenticated browser page for frontend E2E tests.
Requires running Docker services (frontend + API).

Usage:
    pytest tests/e2e/test_search_settings.py -m e2e
"""

import os
import re
import time

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


@pytest.fixture(scope="session")
def authenticated_context(browser):
    """Create an authenticated browser context (login once per session)."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()

    # Navigate to login page
    page.goto(f"{BASE_URL}/login")
    page.wait_for_load_state("networkidle")

    # Click admin login link if present
    admin_link = page.locator(
        "a, button", has_text=re.compile(r"管理者ログイン|Admin Login|管理者")
    )
    if admin_link.count() > 0:
        admin_link.first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1)

    # Fill login form
    page.fill('input[type="text"]', ADMIN_LOGIN_ID)
    page.fill('input[type="password"]', ADMIN_PASSWORD)

    # Check terms checkbox if present
    terms_checkbox = page.locator('input[type="checkbox"], button[role="checkbox"]')
    if terms_checkbox.count() > 0:
        terms_checkbox.first.click()
        time.sleep(0.3)

    # Submit and wait for redirect
    page.click('button[type="submit"]')
    page.wait_for_url("**/workspace/**", timeout=15000)

    page.close()
    yield context
    context.close()


@pytest.fixture
def page(authenticated_context) -> Page:
    """Create a new page in the authenticated context."""
    page = authenticated_context.new_page()
    yield page
    page.close()
