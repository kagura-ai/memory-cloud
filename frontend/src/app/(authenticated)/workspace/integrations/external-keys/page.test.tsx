/**
 * External API keys page — ENABLE_BYOK gate (#1167).
 *
 * The page is gated behind the backend byok feature flag (plan-page pattern
 * #1145): skeleton while flags load, "not available" notice when off, and the
 * key list only fetches when the flag resolves enabled (the API 404s when
 * BYOK is disabled).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (_ns?: string) => (k: string) => k,
}));

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/contexts/MemoryContextContext", () => ({
  useMemoryContext: () => ({ contextId: null }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

let mockFeatures: Record<string, boolean> | null = { byok: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

const mockListKeys = vi.fn();
vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: (...a: unknown[]) => mockListKeys(...a),
  createExternalAPIKey: vi.fn(),
  updateExternalAPIKey: vi.fn(),
  deleteExternalAPIKey: vi.fn(),
  toggleExternalAPIKey: vi.fn(),
}));

import ExternalKeysPage from "./page";

beforeEach(() => {
  vi.clearAllMocks();
  mockFeatures = { byok: true };
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", current_user_role: "owner" },
    currentWorkspaceId: "ws-1",
  });
  mockListKeys.mockResolvedValue([]);
});

afterEach(() => cleanup());

describe("ExternalKeysPage BYOK gate (#1167)", () => {
  it("fetches and renders the page when byok is enabled", async () => {
    render(<ExternalKeysPage />);
    await waitFor(() => expect(mockListKeys).toHaveBeenCalled());
    expect(screen.queryByText("featureDisabled")).toBeNull();
  });

  it("renders the not-available notice and never fetches when byok is off", async () => {
    mockFeatures = { byok: false };
    render(<ExternalKeysPage />);
    expect(screen.getByText("featureDisabled")).toBeInTheDocument();
    expect(mockListKeys).not.toHaveBeenCalled();
  });

  it("holds fetches while feature flags load", () => {
    mockFeatures = null;
    render(<ExternalKeysPage />);
    expect(mockListKeys).not.toHaveBeenCalled();
    expect(screen.queryByText("featureDisabled")).toBeNull();
  });
});
