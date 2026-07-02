/**
 * SearchSettingsSection — ENABLE_BYOK gate (#1167).
 *
 * The reranker-keys probe hits the owner-only /external-keys API, which 404s
 * when BYOK is off; and the "configure reranker keys" CTA links to the
 * external-keys page, which shows a not-available notice in that deployment.
 * Both must be suppressed when features.byok is off.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (_ns?: string) => (k: string) => k,
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

let mockFeatures: Record<string, boolean> | null = { byok: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
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

beforeEach(() => {
  vi.clearAllMocks();
  mockFeatures = { byok: true };
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
    expect(
      screen.getByRole("link", { name: "External API Keys" }),
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
