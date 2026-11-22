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
import { render, screen, waitFor, cleanup, fireEvent, within } from "@testing-library/react";

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
const writes = vi.hoisted(() => ({
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  toggle: vi.fn(),
}));
vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: (...a: unknown[]) => mockListKeys(...a),
  createExternalAPIKey: (...a: unknown[]) => writes.create(...a),
  updateExternalAPIKey: (...a: unknown[]) => writes.update(...a),
  deleteExternalAPIKey: (...a: unknown[]) => writes.remove(...a),
  toggleExternalAPIKey: (...a: unknown[]) => writes.toggle(...a),
}));

import ExternalKeysPage from "./page";
import { useExternalKeysEntry } from "@/hooks/useExternalKeysEntry";

/** The Integrations menus' entry, driven by the same hook they use. */
function MenuEntry() {
  return useExternalKeysEntry() ? <a href="/workspace/integrations/external-keys">menu-entry</a> : null;
}

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
    expect(screen.queryByRole("button", { name: "refresh" })).toBeNull();
    const addButton = await screen.findByRole("button", { name: "addApiKey" });
    expect(screen.getAllByRole("button", { name: "addApiKey" })).toHaveLength(1);
    expect(addButton).toHaveClass("from-brand-green-600");
    expect(screen.getByText("noKeysYet").compareDocumentPosition(addButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).compareDocumentPosition(addButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("answers as a feature that is off when byok is off and no key is stored (#2968)", async () => {
    mockFeatures = { byok: false };
    render(<ExternalKeysPage />);
    expect(await screen.findByText("currentlyUnavailable")).toBeInTheDocument();
    expect(screen.getByText("about.externalKeys")).toBeInTheDocument();
    expect(mockListKeys).toHaveBeenCalledTimes(1);
    // Nothing to manage and nothing to provision: no console at all.
    expect(screen.queryByText("provisioningDisabled")).toBeNull();
    expect(screen.queryByText("noKeysYet")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "addApiKey" })).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    for (const write of Object.values(writes)) expect(write).not.toHaveBeenCalled();
    // The EmptyState primitive carries the notice (frontend rules).
    expect(screen.getByRole("heading", { level: 3, name: "currentlyUnavailable" })).toBeInTheDocument();
  });

  it.each([{ byok: true }, { byok: false }])(
    "shows a failed list load as a page banner, without a toast (%j)",
    async (features) => {
      mockFeatures = features;
      mockListKeys.mockRejectedValue(new Error("down"));
      render(<ExternalKeysPage />);
      expect(await screen.findByText("errorLoadKeys")).toBeInTheDocument();
      expect(mockToast).not.toHaveBeenCalled();
      // A failed read is not "the feature is off".
      expect(screen.queryByText("currentlyUnavailable")).toBeNull();
    },
  );

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

  it("places the single add control before a populated key table", async () => {
    mockListKeys.mockResolvedValue([storedKey()]);
    render(<ExternalKeysPage />);
    const table = await screen.findByRole("table");
    const add = screen.getByRole("button", { name: "addApiKey" });
    expect(screen.getAllByRole("button", { name: "addApiKey" })).toHaveLength(1);
    expect(add.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText("noKeysYet")).toBeNull();
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
        key_name: "COHERE_API_KEY",
        provider: "cohere",
        is_protected: true,
      }),
    ]);
    render(<ExternalKeysPage />);

    expect(await screen.findByText("required")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "deleteApiKey" })).toBeNull();
  });

  it("shows the Edit control for every key when byok is on", async () => {
    mockListKeys.mockResolvedValue([storedKey(), storedKey({ key_name: "COHERE_API_KEY", provider: "cohere", enabled: false })]);
    render(<ExternalKeysPage />);
    await screen.findByRole("table");
    expect(screen.getAllByRole("button", { name: "updateApiKey" })).toHaveLength(2);
    expect(screen.getAllByRole("switch").every((s) => !s.hasAttribute("disabled"))).toBe(true);
    expect(screen.queryByText("currentlyUnavailable")).toBeNull();
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

describe("ExternalKeysPage with BYOK off and stored keys (#2968)", () => {
  const stored = (overrides: Record<string, unknown> = {}) => ({
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

  beforeEach(() => {
    mockFeatures = { byok: false };
  });

  it("is a delete-only list: no add, no edit, no enable", async () => {
    mockListKeys.mockResolvedValue([
      stored(),
      stored({ key_name: "VOYAGE_API_KEY", provider: "voyage", enabled: false }),
    ]);
    render(<ExternalKeysPage />);
    const table = await screen.findByRole("table");
    expect(screen.getByText("provisioningDisabled")).toBeInTheDocument();
    expect(screen.queryByText("currentlyUnavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: "addApiKey" })).toBeNull();
    expect(screen.queryByRole("button", { name: "updateApiKey" })).toBeNull();
    expect(within(table).getAllByRole("button", { name: "deleteApiKey" })).toHaveLength(2);
    const [openaiRow, voyageRow] = within(table).getAllByRole("row").slice(1);
    // Disabling an enabled key stays possible (the toggle route answers with
    // BYOK off); a disabled key cannot be turned back on.
    expect(within(openaiRow).getByRole("switch")).not.toBeDisabled();
    expect(within(voyageRow).getByRole("switch")).toBeDisabled();
    fireEvent.click(within(voyageRow).getByRole("switch"));
    expect(writes.toggle).not.toHaveBeenCalled();
  });

  it("disables an enabled key", async () => {
    mockListKeys.mockResolvedValue([stored()]);
    writes.toggle.mockResolvedValue({});
    render(<ExternalKeysPage />);
    fireEvent.click(await screen.findByRole("switch"));
    await waitFor(() => expect(writes.toggle).toHaveBeenCalledWith("OPENAI_API_KEY", false));
  });

  it("deletes a stored key, then answers as a feature that is off", async () => {
    mockListKeys.mockResolvedValueOnce([stored()]).mockResolvedValue([]);
    writes.remove.mockResolvedValue(undefined);
    render(<ExternalKeysPage />);
    fireEvent.click(await screen.findByRole("button", { name: "deleteApiKey" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "delete" }));
    await waitFor(() => expect(writes.remove).toHaveBeenCalledWith("OPENAI_API_KEY"));
    expect(await screen.findByText("currentlyUnavailable")).toBeInTheDocument();
    expect(writes.create).not.toHaveBeenCalled();
    expect(writes.update).not.toHaveBeenCalled();
  });
});

describe("ExternalKeysPage delete-only list keeps working (#2968 review)", () => {
  const stored = (overrides: Record<string, unknown> = {}) => ({
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

  let rows: ReturnType<typeof stored>[] = [];

  beforeEach(() => {
    mockFeatures = { byok: false };
    rows = [];
    mockListKeys.mockImplementation(async () => rows.map((row) => ({ ...row })));
    writes.remove.mockImplementation(async (name: string) => {
      rows = rows.filter((row) => row.key_name !== name);
    });
  });

  async function deleteRow(name: string) {
    const row = (await screen.findByText(name)).closest("tr");
    if (!row) throw new Error(`no row for ${name}`);
    fireEvent.click(within(row).getByRole("button", { name: "deleteApiKey" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "delete" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }

  it("deletes two stored keys one after the other", async () => {
    rows = [stored(), stored({ key_name: "VOYAGE_API_KEY", provider: "voyage" })];
    render(<ExternalKeysPage />);
    await deleteRow("OPENAI_API_KEY");
    await waitFor(() => expect(screen.queryByText("OPENAI_API_KEY")).toBeNull());
    await deleteRow("VOYAGE_API_KEY");
    expect(writes.remove).toHaveBeenCalledTimes(2);
    expect(writes.remove).toHaveBeenNthCalledWith(2, "VOYAGE_API_KEY");
    expect(await screen.findByText("currentlyUnavailable")).toBeInTheDocument();
  });

  it("lets a disabled protected key be enabled again (#1613), and no other", async () => {
    rows = [
      stored({ is_protected: true, enabled: false }),
      stored({ key_name: "VOYAGE_API_KEY", provider: "voyage", enabled: false }),
    ];
    writes.toggle.mockResolvedValue({});
    render(<ExternalKeysPage />);
    const table = await screen.findByRole("table");
    const [protectedRow, otherRow] = within(table).getAllByRole("row").slice(1);
    expect(within(otherRow).getByRole("switch")).toBeDisabled();
    const protectedSwitch = within(protectedRow).getByRole("switch");
    expect(protectedSwitch).not.toBeDisabled();
    fireEvent.click(protectedSwitch);
    await waitFor(() => expect(writes.toggle).toHaveBeenCalledWith("OPENAI_API_KEY", true));
    expect(writes.toggle).toHaveBeenCalledTimes(1);
  });

  it("drops the Integrations menu entry once the last key is deleted", async () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { id: "ws-presence", current_user_role: "owner" },
      currentWorkspaceId: "ws-presence",
    });
    rows = [stored()];
    render(
      <>
        <MenuEntry />
        <ExternalKeysPage />
      </>,
    );
    expect(await screen.findByText("menu-entry")).toBeInTheDocument();
    await deleteRow("OPENAI_API_KEY");
    expect(await screen.findByText("currentlyUnavailable")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("menu-entry")).toBeNull());
  });
});
