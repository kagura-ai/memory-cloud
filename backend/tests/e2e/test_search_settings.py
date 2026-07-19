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
API_URL = os.environ.get("E2E_API_URL", "http://localhost:8080")

# Use first available context — discovered at session start
_context_id: str | None = None


def _get_context_id(page: Page) -> str:
    """Resolve a context id: first existing link, else self-seed via API.

    #1369: the previous fallback was a hardcoded UUID that only existed in
    one historical dev database — on any fresh workspace (e.g. right after
    ``seed_e2e_admin``) every navigation 404'd and the whole module failed.
    A suite must be self-sufficient: when the contexts page has no
    search-settings link yet, create a context through the API with the
    page's own session cookies.
    """
    global _context_id
    if _context_id:
        return _context_id

    # Env override, else self-seed a context via the API (session cookies
    # from the authenticated page ride along on page.request).
    env_ctx = os.environ.get("E2E_CONTEXT_ID")
    if env_ctx:
        _context_id = env_ctx
        return _context_id

    resp = page.request.post(
        f"{API_URL}/api/v1/contexts",
        data={
            "name": f"e2e-search-settings-{int(time.time())}",
            "display_name": "E2E Search Settings",
        },
    )
    assert resp.ok, f"context self-seed failed: {resp.status} {resp.text()[:200]}"
    _context_id = resp.json()["id"]
    return _context_id


def _search_settings_url(page: Page) -> str:
    # #232 consolidated the standalone /search-settings route into the
    # context detail page's Settings tab (useTabParam deep link).
    ctx_id = _get_context_id(page)
    return f"{BASE_URL}/workspace/contexts/{ctx_id}?tab=settings"


def _navigate(page: Page):
    """Navigate to search settings and wait for load."""
    page.goto(_search_settings_url(page))
    page.wait_for_load_state("networkidle")
    time.sleep(1)


def _enable_reranking(page: Page):
    """Enable the reranking switch if not already on.

    The switch is DISABLED when the workspace is on the free plan or no
    reranker provider is available (no API keys, no self-hosted reranker) —
    an environment capability, not a regression. Skip in that case so the
    provider-dropdown tests only run where they can mean anything.
    """
    switch = page.locator('button[role="switch"]#use_rerank')
    if switch.count() == 0:
        pytest.skip("use_rerank switch not rendered")
    if not switch.first.is_enabled():
        pytest.skip("no reranker provider available in this environment")
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

    def test_settings_tab_active(self, page: Page):
        """#232 replaced the standalone page (and its Back button) with the
        context-detail Settings tab — pin that the deep link lands on it."""
        _navigate(page)
        active_tab = page.locator('[role="tab"][aria-selected="true"]')
        assert active_tab.count() > 0, "No active tab found on context detail page"
        assert re.search(r"Settings|設定", active_tab.first.inner_text() or ""), (
            f"Active tab is not Settings: {active_tab.first.inner_text()!r}"
        )

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
    """Verify sticky save bar appears/hides correctly.

    #232: the bar is render-when-dirty (``{isDirty && ...}``) at
    ``fixed bottom-14`` — not an always-mounted translate-y animation at
    ``bottom-0`` as on the pre-consolidation page.
    """

    _BAR = "div.fixed.bottom-14"

    def test_hidden_initially(self, page: Page):
        _navigate(page)
        assert page.locator(self._BAR).count() == 0, "Sticky save bar rendered without any edit"

    def test_visible_after_change(self, page: Page):
        _navigate(page)
        page.locator("input#semantic_weight").fill("0.50")
        time.sleep(0.5)
        assert page.locator(self._BAR).count() > 0, "Sticky save bar did not appear after an edit"

    def test_hidden_after_discard(self, page: Page):
        _navigate(page)
        page.locator("input#semantic_weight").fill("0.50")
        time.sleep(0.5)
        bar = page.locator(self._BAR)
        bar.locator("button", has_text=re.compile(r"Discard|破棄")).click()
        time.sleep(1)
        assert page.locator(self._BAR).count() == 0, (
            "Sticky save bar still rendered after discarding the edit"
        )


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
        """Locale is the logged-in user's PROFILE preference (#221 → the
        authenticated layout syncs it over any localStorage value), so the
        only reliable switch is PUT /users/profile — restore afterwards."""
        resp = page.request.put(f"{API_URL}/api/v1/users/profile", data={"locale": "ja"})
        assert resp.ok, f"profile locale switch failed: {resp.status}"
        try:
            _navigate(page)
            body_text = page.text_content("body") or ""
            jp_strings = ["ハイブリッド検索", "検索設定", "リランカー", "埋め込み"]
            found = [s for s in jp_strings if s in body_text]
            assert len(found) > 0, f"No Japanese strings found. Checked: {jp_strings}"
        finally:
            page.request.put(f"{API_URL}/api/v1/users/profile", data={"locale": "en"})


# TestResetDialog was removed in #1369: the #158 reset-to-defaults control
# did not survive the #232 consolidation into the context-detail Settings
# tab — there is no Reset button (or AlertDialog) on the surface anymore,
# so the tests asserted a feature that no longer exists.
