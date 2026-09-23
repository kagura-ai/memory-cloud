/**
 * SearchSettingsSection — the free-plan reranker gate renders real copy (#1642).
 *
 * WHY A SECOND FILE: SearchSettingsSection.test.tsx mocks `next-intl` with an
 * identity translator, so the message STRINGS are never formatted there. The
 * bug this pinned lived entirely in the string: the alert used to build its
 * sentence with `t("upgradeToBasic").split("Basic plan")`, which returns a
 * single chunk for every locale whose translation does not contain that
 * English literal — a Japanese reader got the whole sentence, then a hardcoded
 * English "Basic plan" link, then nothing at all from index [1].
 *
 * These tests therefore render against the REAL next-intl provider and the
 * REAL message catalogues, which is the only place that regression is visible.
 *
 * #1643: the upgrade action exists only where the Plan page is reachable
 * (`plan_page` on AND the viewer is the workspace owner), so the mocks below
 * say so; the flag-off cases pin that the explanation survives, still
 * translated, when the action does not.
 *
 * #1646: the alert is now the `reranking` gate's FeatureGateNotice. Its copy is
 * `gate.plan.*` — the tier the matrix names, in this deployment's label, never
 * the old "Basic plan" literal — and its action is a CTA button to the Plan
 * page. A message formatted without an argument it needs would surface here
 * as a next-intl error, so any error fails the test.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { NextIntlClientProvider, type AbstractIntlMessages } from "next-intl";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
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

// #1645: the reranker gate reads the shared tier matrix; the OSS default,
// where the lowest tier has no reranking.
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => OSS_TIERS,
}));
const OSS_TIERS = [
  { name: "free", display_name: "S", reranking: false },
  { name: "basic", display_name: "M", reranking: true },
  { name: "pro", display_name: "L", reranking: true },
  { name: "promax", display_name: "XL", reranking: true },
];

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

/**
 * The notice a free-tier reader must see, from the real catalogue: the tier
 * that has reranking on the OSS matrix is `basic`, labelled "M".
 */
const COPY = {
  en: {
    title: "The M plan includes reranking",
    description: "Upgrade to the M plan to use reranking.",
    action: "Upgrade to M",
  },
  ja: {
    title: "リランキング は M プランで利用できます",
    description: "リランキング を利用するには M プランにアップグレードしてください。",
    action: "M にアップグレード",
  },
} as const;

let intlErrors: string[] = [];

function renderGate(locale: "en" | "ja") {
  const messages = (locale === "ja" ? ja : en) as AbstractIntlMessages;
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={messages}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      <SearchSettingsSection contextId="ctx-1" />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  intlErrors = [];
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

afterEach(() => {
  cleanup();
  // A gate message formatted without an argument it needs lands here.
  expect(intlErrors).toEqual([]);
});

describe("SearchSettingsSection free-plan reranker gate (#1642)", () => {
  it("renders the whole Japanese notice, naming the tier in this deployment's label", async () => {
    renderGate("ja");

    expect(await screen.findByText(COPY.ja.title)).toBeInTheDocument();
    expect(screen.getByText(COPY.ja.description)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: COPY.ja.action }));
    expect(mockPush).toHaveBeenCalledWith(PLAN_HREF);

    // No tier literal from the old copy reaches a ja reader.
    expect(document.body.textContent).not.toContain("Basic");
  });

  it("renders the English notice with the upgrade CTA", async () => {
    renderGate("en");

    expect(await screen.findByText(COPY.en.title)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.description)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: COPY.en.action }));
    expect(mockPush).toHaveBeenCalledWith(PLAN_HREF);
    expect(document.body.textContent).not.toContain("Basic plan");
  });
});

describe("SearchSettingsSection reranker gate, CTA withheld (#1643)", () => {
  it("ja: renders the whole notice with no CTA when plan_page is off", async () => {
    mockPlanPage = false;
    renderGate("ja");

    // Same explanation, still translated — just nothing to click.
    expect(await screen.findByText(COPY.ja.title)).toBeInTheDocument();
    expect(screen.getByText(COPY.ja.description)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: COPY.ja.action }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${PLAN_HREF}"]`)).toBeNull();
  });

  it("en: renders the whole notice with no CTA for a non-owner", async () => {
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

    expect(await screen.findByText(COPY.en.title)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.description)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: COPY.en.action }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${PLAN_HREF}"]`)).toBeNull();
  });
});
