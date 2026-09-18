/**
 * SearchSettingsSection — ENABLE_BYOK gate (#1167) and deployment default (#1572).
 *
 * #1167: the reranker-keys probe hits the owner-only /external-keys API, which
 * 404s when BYOK is off; and the "configure reranker keys" CTA links to the
 * external-keys page, which shows a not-available notice in that deployment.
 * Both must be suppressed when features.byok is off.
 *
 * #1572: the card renders the deployment default from /system/info
 * (`search_defaults`), is disabled with a note when `features.reranking` is
 * false, and hides the configure-keys CTA when the default is the keyless
 * self_hosted reranker and it is reachable.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

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
    // Not disabled by deployment while unknown.
    expect(
      screen.queryByText("rerankingDisabledByDeployment"),
    ).not.toBeInTheDocument();
  });

  it("disables the card and shows the note when features.reranking is false", async () => {
    mockFeatures = { byok: true, reranking: false };
    mockInfo = {
      features: { byok: true, reranking: false },
      search_defaults: VOYAGE_DEFAULTS,
    };
    render(<SearchSettingsSection contextId="ctx-1" />);
    expect(
      await screen.findByText("rerankingDisabledByDeployment"),
    ).toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeDisabled();
    // The "no keys" prompt is noise when the deployment never reranks.
    expect(screen.queryByText("noRerankerKeys")).not.toBeInTheDocument();
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
