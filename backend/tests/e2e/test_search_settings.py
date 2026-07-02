"""E2E tests for Search Settings page (#158).

Verifies the unified search-settings UX including:
- Sticky save bar behavior
- Self-hosted reranker provider support
- Provider availability badges
- NaN guard on number inputs
- Dark mode styling
- i18n (Japanese locale)
- AlertDialog for reset confirmation

Requires running Docker services:
    docker compose up -d

Run:
    pytest tests/e2e/test_search_settings.py -m e2e -v
"""

import os
import re
import time

import pytest
from playwright.sync_api import Page

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")

# Use first available context — discovered at session start
_context_id: str | None = None


def _get_context_id(page: Page) -> str:
    """Navigate to contexts list and grab the first context ID from the URL."""
    global _context_id
    if _context_id:
        return _context_id

    page.goto(f"{BASE_URL}/workspace/contexts")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Click the first settings link/icon
    settings_link = page.locator('a[href*="/search-settings"]').first
    if settings_link.count() > 0:
        href = settings_link.get_attribute("href") or ""
        # Extract context ID from /workspace/contexts/{id}/search-settings
        match = re.search(r"/contexts/([a-f0-9-]+)/search-settings", href)
        if match:
            _context_id = match.group(1)
            return _context_id

    # Fallback: use env var or hardcoded dev context
    _context_id = os.environ.get("E2E_CONTEXT_ID", "700a6873-ade0-44ba-beca-95e8cda3ad82")
    return _context_id


def _search_settings_url(page: Page) -> str:
    ctx_id = _get_context_id(page)
    return f"{BASE_URL}/workspace/contexts/{ctx_id}/search-settings"


def _navigate(page: Page):
    """Navigate to search settings and wait for load."""
    page.goto(_search_settings_url(page))
    page.wait_for_load_state("networkidle")
    time.sleep(1)


def _enable_reranking(page: Page):
    """Enable the reranking switch if not already on."""
    switch = page.locator('button[role="switch"]#use_rerank')
    if switch.count() > 0:
        is_checked = (
            switch.get_attribute("data-state") == "checked"
            or switch.get_attribute("aria-checked") == "true"
        )
        if not is_checked:
            switch.click()
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


class TestPageStructure:
    """Verify basic page layout after #158 rewrite."""

    def test_cards_rendered(self, page: Page):
        _navigate(page)
        cards = page.locator(
            '[data-slot="card"], [class*="card"], '
            '[class*="rounded-xl border"], [class*="rounded-lg border"]'
        )
        assert cards.count() >= 3, f"Expected ≥3 cards, found {cards.count()}"

    def test_back_button_present(self, page: Page):
        _navigate(page)
        back = page.locator("button", has_text=re.compile(r"Back|戻る"))
        assert back.count() > 0

    def test_refresh_button_removed(self, page: Page):
        _navigate(page)
        refresh = page.locator("button", has_text=re.compile(r"^Refresh$|^更新$"))
        assert refresh.count() == 0, "Refresh button should be removed in #158"

    def test_no_save_in_header(self, page: Page):
        _navigate(page)
        header = page.locator("header, [class*='PageHeader']").first
        if header.count() > 0:
            save = header.locator("button", has_text=re.compile(r"Save|保存"))
            assert save.count() == 0, "Save should be in sticky bar, not header"


class TestStickySaveBar:
    """Verify sticky save bar appears/hides correctly."""

    def test_hidden_initially(self, page: Page):
        _navigate(page)
        bar = page.locator("div.fixed.bottom-0")
        assert bar.count() > 0
        assert "translate-y-full" in (bar.get_attribute("class") or "")

    def test_visible_after_change(self, page: Page):
        _navigate(page)
        page.locator("input#semantic_weight").fill("0.50")
        time.sleep(0.5)
        bar = page.locator("div.fixed.bottom-0")
        assert "translate-y-0" in (bar.get_attribute("class") or "")

    def test_hidden_after_discard(self, page: Page):
        _navigate(page)
        page.locator("input#semantic_weight").fill("0.50")
        time.sleep(0.5)
        bar = page.locator("div.fixed.bottom-0")
        bar.locator("button", has_text=re.compile(r"Discard|破棄")).click()
        time.sleep(1)
        assert "translate-y-full" in (bar.get_attribute("class") or "")


class TestRerankerProviders:
    """Verify Self-hosted, Voyage, Cohere provider dropdown."""

    def test_all_providers_listed(self, page: Page):
        _navigate(page)
        _enable_reranking(page)

        trigger = page.locator("#reranker_provider")
        assert trigger.count() > 0, "Provider dropdown not found"
        trigger.click()
        time.sleep(0.5)

        assert page.locator('[role="option"]', has_text="Self-hosted").count() > 0
        assert page.locator('[role="option"]', has_text="Voyage").count() > 0
        assert page.locator('[role="option"]', has_text="Cohere").count() > 0
        page.keyboard.press("Escape")

    def test_status_badges_shown(self, page: Page):
        _navigate(page)
        _enable_reranking(page)
        page.locator("#reranker_provider").click()
        time.sleep(0.5)

        badges = page.locator('[role="option"] .inline-flex, [role="option"] [class*="badge"]')
        assert badges.count() >= 2, f"Expected ≥2 badges, found {badges.count()}"
        page.keyboard.press("Escape")

    def test_voyage_disabled_without_key(self, page: Page):
        _navigate(page)
        _enable_reranking(page)
        page.locator("#reranker_provider").click()
        time.sleep(0.5)

        voyage = page.locator('[role="option"]', has_text="Voyage")
        assert voyage.count() > 0
        is_disabled = (
            voyage.get_attribute("data-disabled") is not None
            or voyage.get_attribute("aria-disabled") == "true"
        )
        assert is_disabled, "Voyage should be disabled without API key"
        page.keyboard.press("Escape")


class TestNaNGuard:
    """Verify NaN protection on number inputs."""

    def test_semantic_weight_nan_guard(self, page: Page):
        _navigate(page)
        inp = page.locator("input#semantic_weight")
        inp.fill("")
        time.sleep(0.3)
        val = inp.input_value()
        assert "NaN" not in val, f"NaN leaked into semantic weight: {val}"

    def test_fetch_factor_nan_guard(self, page: Page):
        _navigate(page)
        inp = page.locator("input#fetch_factor")
        inp.fill("")
        time.sleep(0.3)
        val = inp.input_value()
        assert "NaN" not in val, f"NaN leaked into fetch factor: {val}"


class TestDarkMode:
    """Verify dark mode classes are present."""

    def test_dark_classes_exist(self, page: Page):
        _navigate(page)
        page.evaluate("document.documentElement.classList.add('dark')")
        time.sleep(0.5)

        dark = page.locator('[class*="dark:bg-gray-900"], [class*="dark:bg-blue-950"]')
        assert dark.count() > 0, "No dark: prefixed elements found"

        page.evaluate("document.documentElement.classList.remove('dark')")


class TestI18n:
    """Verify Japanese locale renders."""

    def test_japanese_strings(self, page: Page):
        page.goto(f"{_search_settings_url(page)}?locale=ja")
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        body_text = page.text_content("body") or ""
        jp_strings = ["ハイブリッド検索", "検索設定", "リランカー", "埋め込み"]
        found = [s for s in jp_strings if s in body_text]
        assert len(found) > 0, f"No Japanese strings found. Checked: {jp_strings}"


class TestResetDialog:
    """Verify AlertDialog for reset confirmation (not window.confirm)."""

    def test_alert_dialog_shown(self, page: Page):
        _navigate(page)
        reset_btn = page.locator("button", has_text=re.compile(r"Reset|デフォルト"))
        assert reset_btn.count() > 0, "Reset button not found"
        reset_btn.click()
        time.sleep(0.5)

        dialog = page.locator('[role="alertdialog"]')
        assert dialog.count() > 0, "AlertDialog not shown"

    def test_dialog_has_cancel(self, page: Page):
        _navigate(page)
        page.locator("button", has_text=re.compile(r"Reset|デフォルト")).click()
        time.sleep(0.5)

        dialog = page.locator('[role="alertdialog"]')
        cancel = dialog.locator("button", has_text=re.compile(r"Cancel|キャンセル"))
        assert cancel.count() > 0, "Cancel button not found in dialog"
        cancel.click()
