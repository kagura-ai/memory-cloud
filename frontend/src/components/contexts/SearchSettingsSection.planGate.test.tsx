/**
 * SearchSettingsSection — the free-plan reranker gate renders real copy (#1642).
 *
 * WHY A SECOND FILE: SearchSettingsSection.test.tsx mocks `next-intl` with an
 * identity translator, so the message STRINGS are never formatted there. The
 * bug this pins lived entirely in the string: the alert used to build its
 * sentence with `t("upgradeToBasic").split("Basic plan")`, which returns a
 * single chunk for every locale whose translation does not contain that
 * English literal — a Japanese reader got the whole sentence, then a hardcoded
 * English "Basic plan" link, then nothing at all from index [1].
 *
 * These tests therefore render against the REAL next-intl provider and the
 * REAL message catalogues, which is the only place that regression is visible.
 *
 * #1643: the <link> chunk is now a real <Link> only where the Plan page is
 * reachable (`plan_page` on AND the viewer is the workspace owner), so the
 * mocks below say so; the flag-off cases pin that the SENTENCE survives —
 * link text included, still translated — when the link does not.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { NextIntlClientProvider, type AbstractIntlMessages } from "next-intl";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

const SEARCH_DEFAULTS = {
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};
/** #1643: `plan_page` decides whether the chunk is a link. */
let mockPlanPage = true;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({
    byok: true,
    reranking: true,
    plan_page: mockPlanPage,
  }),
  useSystemInfo: () => ({
    features: { byok: true, reranking: true, plan_page: mockPlanPage },
    search_defaults: SEARCH_DEFAULTS,
  }),
}));

const mockGetConfig = vi.fn();
vi.mock("@/lib/api/contexts", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    getContextSearchConfig: (...a: unknown[]) => mockGetConfig(...a),
    updateContextSearchConfig: vi.fn(),
  };
});

vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ services: {} }) },
  ApiError: class ApiError extends Error {},
}));

import { SearchSettingsSection } from "./SearchSettingsSection";

const PLAN_HREF = "/workspace/settings/plan";

/** The whole sentence a reader must see, link text included, in one piece. */
const EN_SENTENCE =
  "Upgrade to Basic plan to enable AI-powered reranking for improved search quality.";
const EN_LINK = "Basic plan";
const JA_SENTENCE =
  "Basicプランにアップグレードして、AI搭載リランキングで検索品質を向上させましょう。";
const JA_LINK = "Basicプラン";

/**
 * Match the INNERMOST element whose full text content is `text`.
 *
 * The default text matcher only looks at an element's own text nodes, so a
 * sentence interrupted by a <Link> never matches it — which is exactly the
 * shape this fix produces. Comparing `textContent` and rejecting any element
 * that has a child with the same text keeps the match on the <p>.
 */
const wholeText =
  (text: string) => (_content: string, element: Element | null) => {
    const normalize = (value: string | null) =>
      (value ?? "").replace(/\s+/g, " ").trim();
    if (!element || normalize(element.textContent) !== text) return false;
    return !Array.from(element.children).some(
      (child) => normalize(child.textContent) === text,
    );
  };

function renderGate(locale: "en" | "ja") {
  const messages = (locale === "ja" ? ja : en) as AbstractIntlMessages;
  return render(
    <NextIntlClientProvider locale={locale} messages={messages} timeZone="UTC">
      <SearchSettingsSection contextId="ctx-1" />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockPlanPage = true;
  // Free workspace → the upgrade alert is the gate that renders. The owner
  // role is what #1643 requires for the CTA half of it.
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: {
      id: "ws-1",
      plan_name: "free",
      current_user_role: "owner",
    },
    currentWorkspaceId: "ws-1",
    loading: false,
  });
  mockGetConfig.mockResolvedValue({
    context_id: "ctx-1",
    semantic_weight: 0.6,
    bm25_weight: 0.4,
    fetch_factor: 3,
    use_rerank: false,
    reranker_provider: "voyage",
    reranker_model: "rerank-2",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  });
});

afterEach(() => cleanup());

describe("SearchSettingsSection free-plan reranker gate (#1642)", () => {
  it("renders the whole Japanese sentence with the link inside it", async () => {
    renderGate("ja");

    // One element, one complete sentence — the tail after the link included.
    const sentence = await screen.findByText(wholeText(JA_SENTENCE));

    const link = screen.getByRole("link", { name: JA_LINK });
    expect(link).toHaveAttribute("href", PLAN_HREF);
    expect(sentence).toContainElement(link);

    // The English literal the old split() keyed on must not reach a ja reader.
    expect(document.body.textContent).not.toContain(EN_LINK);
  });

  it("still renders the English sentence with a linked plan name", async () => {
    renderGate("en");

    const sentence = await screen.findByText(wholeText(EN_SENTENCE));

    const link = screen.getByRole("link", { name: EN_LINK });
    expect(link).toHaveAttribute("href", PLAN_HREF);
    expect(sentence).toContainElement(link);
  });
});

describe("SearchSettingsSection reranker gate, CTA withheld (#1643)", () => {
  it("ja: renders the whole sentence as plain text when plan_page is off", async () => {
    mockPlanPage = false;
    renderGate("ja");

    // Same sentence, link text included and still translated — just not a link.
    const sentence = await screen.findByText(wholeText(JA_SENTENCE));
    expect(sentence.textContent).toContain(JA_LINK);
    expect(
      screen.queryByRole("link", { name: JA_LINK }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${PLAN_HREF}"]`)).toBeNull();
  });

  it("en: renders the whole sentence as plain text for a non-owner", async () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: "ws-1",
        plan_name: "free",
        current_user_role: "admin",
      },
      currentWorkspaceId: "ws-1",
      loading: false,
    });
    renderGate("en");

    const sentence = await screen.findByText(wholeText(EN_SENTENCE));
    expect(sentence.textContent).toContain(EN_LINK);
    expect(
      screen.queryByRole("link", { name: EN_LINK }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${PLAN_HREF}"]`)).toBeNull();
  });
});

describe("searchSettings.upgradeToBasic message", () => {
  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)("%s wraps the link text in a <link> tag", (_locale, messages) => {
    expect(messages.searchSettings.upgradeToBasic).toMatch(
      /<link>[^<]+<\/link>/,
    );
  });
});
