/**
 * SearchSettingsSection — ENABLE_BYOK gate (#1167) and deployment default (#1572).
 *
 * #1167: the reranker-keys probe hits the owner-only /external-keys API, which
 * 404s when BYOK is off; and the "configure reranker keys" CTA links to the
 * external-keys page, which shows a not-available notice in that deployment.
 * Both must be suppressed when features.byok is off.
 *
 * #1572: the card renders the deployment default from /system/info
 * (`search_defaults`) and hides the configure-keys CTA when the default is the
 * keyless self_hosted reranker and it is reachable.
 *
 * #1580: when `features.reranking` is false the reranker card is not rendered
 * at all (hidden, not greyed out) and the external-keys probe is skipped;
 * `true` and unknown (still loading / older backend) keep the card.
 *
 * #1643: the free-tier upgrade sentence keeps its <link> chunk only where the
 * Plan page is reachable. `plan_page` stays ABSENT from the beforeEach default
 * (the self-hosted truth), so the cases below opt in explicitly.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";
import type { PlanTierFeature } from "@/lib/api/workspaces";

vi.mock("next-intl", () => ({
  // Identity translator that appends interpolation values as
  // `key(a=1,b=2)`, so deployment-default copy is assertable; plus a t.rich
  // that invokes the <link> tag renderer with the key as chunks, so the CTA
  // Link actually renders in tests.
  useTranslations: (_ns?: string) => {
    const t = ((k: string, values?: Record<string, unknown>) =>
      values
        ? `${k}(${Object.entries(values)
            .map(([name, v]) => `${name}=${String(v)}`)
            .join(",")})`
        : k) as ((k: string, values?: Record<string, unknown>) => string) & {
      rich: (
        k: string,
        values?: Record<string, (chunks: string) => unknown>,
      ) => unknown;
    };
    t.rich = (k, values) => (values?.link ? values.link(k) : k);
    return t;
  },
  useLocale: () => "en",
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

interface MockInfo {
  features: Record<string, boolean>;
  search_defaults?: {
    use_rerank: boolean;
    reranker_provider: string;
    reranker_model: string;
  };
}
let mockFeatures: Record<string, boolean> | null = { byok: true };
let mockInfo: MockInfo | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
  useSystemInfo: () => mockInfo,
}));

// #1645: the reranker gate reads the shared tier matrix (`null` = still
// resolving). Default: the OSS matrix, so `plan_name` decides exactly as the
// tier's row does (reranking from M up).
const OSS_TIERS = [
  { name: "free", display_name: "S", reranking: false },
  { name: "basic", display_name: "M", reranking: true },
  { name: "pro", display_name: "L", reranking: true },
  { name: "promax", display_name: "XL", reranking: true },
] as unknown as PlanTierFeature[];
let mockTiers: PlanTierFeature[] | null = OSS_TIERS;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
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

const mockListKeys = vi.fn();
vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: (...a: unknown[]) => mockListKeys(...a),
}));

const mockApiGet = vi.fn();
vi.mock("@/lib/api/base", () => ({
  apiClient: { get: (...a: unknown[]) => mockApiGet(...a) },
  ApiError: class ApiError extends Error {},
}));

import { SearchSettingsSection } from "./SearchSettingsSection";

const VOYAGE_DEFAULTS = {
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
};
const SELF_HOSTED_DEFAULTS = {
  use_rerank: true,
  reranker_provider: "self_hosted",
  reranker_model: "qwen3-reranker-0.6b",
};

beforeEach(() => {
  vi.clearAllMocks();
  mockTiers = OSS_TIERS;
  mockFeatures = { byok: true };
  mockInfo = { features: { byok: true }, search_defaults: VOYAGE_DEFAULTS };
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", plan_name: "basic" },
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
  mockListKeys.mockResolvedValue([]);
  // telemetry probe: self-hosted reranker not available
  mockApiGet.mockResolvedValue({ services: {} });
});

afterEach(() => cleanup());

describe("SearchSettingsSection BYOK gate (#1167)", () => {
  it("probes external keys and shows the configure-keys CTA when byok is on", async () => {
    render(<SearchSettingsSection contextId="ctx-1" />);
    await waitFor(() => expect(mockListKeys).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText("noRerankerKeys")).toBeInTheDocument(),
    );
    // The mocked t.rich renders the <link> tag with the message key as chunks.
    expect(
      screen.getByRole("link", { name: "configureRerankerKeys" }),
    ).toHaveAttribute("href", "/workspace/integrations/external-keys");
  });

  it("skips the probe and renders no external-keys link when byok is off", async () => {
    mockFeatures = { byok: false };
    const { container } = render(<SearchSettingsSection contextId="ctx-1" />);
    // Let the config/telemetry loads settle.
    await waitFor(() => expect(mockGetConfig).toHaveBeenCalled());
    expect(mockListKeys).not.toHaveBeenCalled();
    expect(
      container.querySelector(
        'a[href="/workspace/integrations/external-keys"]',
      ),
    ).toBeNull();
  });
});

describe("SearchSettingsSection deployment default (#1572)", () => {
  it("renders the deployment default provider/model and on/off state", async () => {
    mockInfo = {
      features: { byok: true },
      search_defaults: SELF_HOSTED_DEFAULTS,
    };
    render(<SearchSettingsSection contextId="ctx-1" />);
    await waitFor(() => expect(mockGetConfig).toHaveBeenCalled());

    // self_hosted is labelled via the i18n key; the model is the served name.
    expect(
      await screen.findByText(
        "rerankerConfigDesc(provider=selfHostedLocal,model=qwen3-reranker-0.6b)",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("enableRerankingDesc(state=deploymentDefaultOn)"),
    ).toBeInTheDocument();
  });

  it("labels a voyage default and an off state", async () => {
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(
      await screen.findByText(
        "rerankerConfigDesc(provider=Voyage AI,model=rerank-2)",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("enableRerankingDesc(state=deploymentDefaultOff)"),
    ).toBeInTheDocument();
  });

  it("shows the plain copy while /system/info is still loading", async () => {
    mockFeatures = null;
    mockInfo = null;
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(
      await screen.findByText("rerankerConfigDescPlain"),
    ).toBeInTheDocument();
    expect(screen.getByText("enableRerankingDescPlain")).toBeInTheDocument();
  });
  it("hides the configure-keys CTA when the deployment default is keyless self_hosted and reachable", async () => {
    mockInfo = {
      features: { byok: true },
      search_defaults: SELF_HOSTED_DEFAULTS,
    };
    // Reachable local reranker; the context still points at voyage without a key.
    mockApiGet.mockResolvedValue({
      services: { self_hosted: { status: "ok" } },
    });
    mockGetConfig.mockResolvedValue({
      context_id: "ctx-1",
      semantic_weight: 0.6,
      bm25_weight: 0.4,
      fetch_factor: 3,
      use_rerank: true,
      reranker_provider: "voyage",
      reranker_model: "rerank-2",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });

    const { container } = render(<SearchSettingsSection contextId="ctx-1" />);
    await waitFor(() => expect(mockListKeys).toHaveBeenCalled());
    // The provider-unavailable alert renders the headline without the link.
    expect(await screen.findByText("noRerankerKeys")).toBeInTheDocument();
    expect(
      container.querySelector(
        'a[href="/workspace/integrations/external-keys"]',
      ),
    ).toBeNull();
  });
});

describe("SearchSettingsSection reranking off on this deployment (#1580)", () => {
  const RERANKING_CONTEXT = {
    context_id: "ctx-1",
    semantic_weight: 0.6,
    bm25_weight: 0.4,
    fetch_factor: 3,
    use_rerank: true,
    reranker_provider: "self_hosted",
    reranker_model: "bge-reranker-v2-m3",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };

  const setReranking = (reranking: boolean) => {
    mockFeatures = { byok: true, reranking };
    mockInfo = {
      features: { byok: true, reranking },
      search_defaults: VOYAGE_DEFAULTS,
    };
  };

  it("does not render the reranker card when features.reranking is false", async () => {
    setReranking(false);
    // A context that enabled reranking before the deployment turned it off
    // must not bring the provider/model controls back either.
    mockGetConfig.mockResolvedValue(RERANKING_CONTEXT);
    render(<SearchSettingsSection contextId="ctx-1" />);

    // The rest of the section is still there.
    expect(await screen.findByText("hybridSearchWeights")).toBeInTheDocument();
    expect(screen.getByText("embeddingConfig")).toBeInTheDocument();

    expect(screen.queryByText("rerankerConfig")).not.toBeInTheDocument();
    expect(screen.queryByText("enableReranking")).not.toBeInTheDocument();
    expect(screen.queryByText("noRerankerKeys")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
  });

  it("makes no external-keys request when features.reranking is false", async () => {
    setReranking(false);
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(await screen.findByText("hybridSearchWeights")).toBeInTheDocument();
    // The telemetry probe still runs; give a stray keys probe the same chance.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalled());
    expect(mockListKeys).not.toHaveBeenCalled();
  });

  it("renders the card and probes keys when features.reranking is true", async () => {
    setReranking(true);
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(await screen.findByText("rerankerConfig")).toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeInTheDocument();
    await waitFor(() => expect(mockListKeys).toHaveBeenCalled());
  });

  it("renders the card while the flag is unknown (older backend without it)", async () => {
    // beforeEach: features = { byok: true } — no `reranking` key at all.
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(await screen.findByText("rerankerConfig")).toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeInTheDocument();
  });

  it("drops the reranking sentence from the impact box only when reranking is off", async () => {
    setReranking(false);
    const { unmount } = render(<SearchSettingsSection contextId="ctx-1" />);
    expect(
      await screen.findByText("impactOnQualityDescNoRerank"),
    ).toBeInTheDocument();
    expect(screen.queryByText("impactOnQualityDesc")).not.toBeInTheDocument();
    unmount();

    setReranking(true);
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(await screen.findByText("impactOnQualityDesc")).toBeInTheDocument();
    expect(
      screen.queryByText("impactOnQualityDescNoRerank"),
    ).not.toBeInTheDocument();
  });

  it("does not block Save on a hidden reranker provider", async () => {
    setReranking(false);
    // self_hosted is unreachable (telemetry mock) — with the card visible this
    // blocks Save; hidden, the user could neither see nor fix the cause.
    mockGetConfig.mockResolvedValue(RERANKING_CONTEXT);
    render(<SearchSettingsSection contextId="ctx-1" />);

    fireEvent.change(await screen.findByLabelText("fetchFactorLabel"), {
      target: { value: "5" },
    });

    expect(await screen.findByText("unsavedChangesBar")).toBeInTheDocument();
    expect(
      screen.queryByText("providerUnavailableCannotSave"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "saveChanges" })).toBeEnabled();
  });
});

describe("SearchSettingsSection free-tier upgrade CTA (#1643)", () => {
  function setFree(role: string) {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: "ws-1",
        plan_name: "free",
        current_user_role: role,
      },
      currentWorkspaceId: "ws-1",
      loading: false,
    });
  }

  it("free tier, plan_page off: the reranker notice renders as plain text with no plan link", async () => {
    setFree("owner");
    render(<SearchSettingsSection contextId="ctx-1" />);

    // The notice and the whole sentence survive — only the link does not.
    expect(
      await screen.findByText("rerankerNotAvailableFree"),
    ).toBeInTheDocument();
    expect(screen.getByText("upgradeToBasic")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "upgradeToBasic" }),
    ).not.toBeInTheDocument();
  });

  it("free tier, plan_page on but not the owner: still no plan link", async () => {
    mockFeatures = { byok: true, plan_page: true };
    mockInfo = {
      features: { byok: true, plan_page: true },
      search_defaults: VOYAGE_DEFAULTS,
    };
    setFree("admin");
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(await screen.findByText("upgradeToBasic")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "upgradeToBasic" }),
    ).not.toBeInTheDocument();
  });

  it("free tier, owner on a plan_page deployment: the notice links to the plan page", async () => {
    mockFeatures = { byok: true, plan_page: true };
    mockInfo = {
      features: { byok: true, plan_page: true },
      search_defaults: VOYAGE_DEFAULTS,
    };
    setFree("owner");
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(
      await screen.findByRole("link", { name: "upgradeToBasic" }),
    ).toHaveAttribute("href", "/workspace/settings/plan");
  });

  it("free tier, /system/info still pending: no plan link", async () => {
    mockFeatures = null;
    setFree("owner");
    render(<SearchSettingsSection contextId="ctx-1" />);

    // #1645: the reranker gate also reads the deployment's `reranking` flag,
    // so while /system/info is in flight the gate is pending: the card stays
    // (#1580), its controls wait, and nothing is upsold — link included.
    expect(await screen.findByText("rerankerConfig")).toBeInTheDocument();
    expect(screen.queryByText("rerankerNotAvailableFree")).toBeNull();
    expect(
      screen.queryByRole("link", { name: "upgradeToBasic" }),
    ).not.toBeInTheDocument();
  });
});

describe("SearchSettingsSection reads the reranking gate (#1645)", () => {
  beforeEach(() => {
    // BYOK off: no key probe, so a provider is never "unavailable" and the
    // controls' only remaining input is the gate itself.
    mockFeatures = { byok: false };
    mockInfo = { features: { byok: false }, search_defaults: VOYAGE_DEFAULTS };
  });

  it("reranker controls are inert while the matrix resolves", async () => {
    mockTiers = null;
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(await screen.findByRole("switch")).toBeDisabled();
    // Pending is not a refusal: no free-tier notice either.
    expect(screen.queryByText("rerankerNotAvailableFree")).toBeNull();
  });

  it("the reranker controls work once the matrix says this tier has it", async () => {
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(await screen.findByRole("switch")).not.toBeDisabled();
  });

  it("a tier without reranking: inert controls and the free-tier notice", async () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { id: "ws-1", plan_name: "free" },
    });
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(await screen.findByRole("switch")).toBeDisabled();
    expect(screen.getByText("rerankerNotAvailableFree")).toBeInTheDocument();
  });

  it("follows the matrix, not the tier name: an operator gives free reranking", async () => {
    mockTiers = OSS_TIERS.map((t) =>
      t.name === "free" ? { ...t, reranking: true } : t,
    );
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { id: "ws-1", plan_name: "free" },
    });
    render(<SearchSettingsSection contextId="ctx-1" />);

    expect(await screen.findByRole("switch")).not.toBeDisabled();
    expect(screen.queryByText("rerankerNotAvailableFree")).toBeNull();
  });

  it("an explicit reranking: false hides the card whatever the matrix says", async () => {
    mockTiers = null;
    mockFeatures = { byok: true, reranking: false };
    mockInfo = {
      features: { byok: true, reranking: false },
      search_defaults: VOYAGE_DEFAULTS,
    };
    render(<SearchSettingsSection contextId="ctx-1" />);

    // The config card still renders; the reranker card does not.
    await waitFor(() => expect(mockGetConfig).toHaveBeenCalled());
    expect(screen.queryByText("rerankerConfig")).toBeNull();
  });
});
