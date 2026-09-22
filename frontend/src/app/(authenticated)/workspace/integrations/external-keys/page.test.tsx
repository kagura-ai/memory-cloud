/**
 * External API keys page — ENABLE_BYOK gate (#1167).
 *
 * The backend byok feature flag gates provisioning only: skeleton while flags
 * load, and with the flag off the page stays a management console for keys
 * stored earlier (list / disable / delete) with a provisioning-disabled notice
 * in place of the Add / Edit affordances. The key list fetches regardless of
 * the flag — the list route answers with BYOK off.
 *
 * #1613: the "Required" badge follows the API's `is_protected`, not the
 * provider — an OpenAI key nothing reads keeps its delete and disable controls.
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

// The embedding-status probe is not under test; keep it off the network.
vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: vi.fn().mockResolvedValue({}),
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
    expect(screen.queryByText("provisioningDisabled")).toBeNull();
  });

  it("keeps a management console (fetches + no full block) when byok is off", async () => {
    // v0.42 review #32: BYOK-off gates only provisioning — an owner keeps a
    // list + disable/delete console for already-stored keys, with a
    // provisioning-disabled banner instead of the whole-page block.
    mockFeatures = { byok: false };
    render(<ExternalKeysPage />);
    await waitFor(() => expect(mockListKeys).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText("provisioningDisabled")).toBeInTheDocument(),
    );
    // The create affordance is hidden; the empty-state "add first key" is gone.
    expect(screen.queryByText("addApiKey")).toBeNull();
  });

  it("shows a loading skeleton (no notice) while feature flags load", () => {
    // v0.42 review #32: the key list (GET) is byok-independent, so it may fetch
    // during the flag-loading window; while systemFeatures is null the page
    // renders the skeleton and no provisioning notice.
    mockFeatures = null;
    render(<ExternalKeysPage />);
    expect(screen.queryByText("provisioningDisabled")).toBeNull();
  });
});

describe("ExternalKeysPage protection (#1613)", () => {
  const storedKey = (overrides: Record<string, unknown> = {}) => ({
    id: 1,
    key_name: "OPENAI_API_KEY",
    provider: "openai",
    masked_value: "sk-...abcd",
    enabled: true,
    is_protected: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    updated_by: null,
    ...overrides,
  });

  it("shows the Required badge and locks the controls only when is_protected", async () => {
    mockListKeys.mockResolvedValue([storedKey({ is_protected: true })]);
    render(<ExternalKeysPage />);

    expect(await screen.findByText("required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "deleteApiKey" })).toBeNull();
    expect(screen.getByRole("switch")).toBeDisabled();
  });

  it("offers delete and disable for an OpenAI key that is not protected", async () => {
    mockListKeys.mockResolvedValue([storedKey()]);
    render(<ExternalKeysPage />);

    expect(
      await screen.findByRole("button", { name: "deleteApiKey" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("required")).toBeNull();
    expect(screen.getByRole("switch")).not.toBeDisabled();
  });

  it("does not decide from the provider: a protected flag on any key is honoured", async () => {
    mockListKeys.mockResolvedValue([
      storedKey({
        id: 2,
        key_name: "COHERE_API_KEY",
        provider: "cohere",
        is_protected: true,
      }),
    ]);
    render(<ExternalKeysPage />);

    expect(await screen.findByText("required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "deleteApiKey" })).toBeNull();
  });

  it("keeps the delete control when byok is off (nothing is protected then)", async () => {
    mockFeatures = { byok: false };
    mockListKeys.mockResolvedValue([storedKey()]);
    render(<ExternalKeysPage />);

    expect(
      await screen.findByRole("button", { name: "deleteApiKey" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("required")).toBeNull();
    expect(screen.getByText("provisioningDisabled")).toBeInTheDocument();
  });

  it("lets a protected key that is currently disabled be re-enabled", async () => {
    mockListKeys.mockResolvedValue([
      storedKey({ is_protected: true, enabled: false }),
    ]);
    render(<ExternalKeysPage />);

    expect(await screen.findByText("required")).toBeInTheDocument();
    expect(screen.getByRole("switch")).not.toBeDisabled();
  });
});
