/**
 * Tests for APIKeysTabPanel table view (Issue #943).
 *
 * Covers the gate1 scope for the card → table conversion:
 *   - API keys render as a table with the expected column headers
 *   - "Last used" shows a value for a key that has authenticated
 *   - "Last used" renders "—" for a key whose last_used_at is null
 *
 * Both the desktop table and the mobile card fallback are present in the DOM
 * under happy-dom (CSS `hidden`/`md:block` does not remove nodes), so the
 * "Last used" assertions are scoped to the table via `within(getByRole("table"))`.
 */

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  APIKeysTabPanel,
  clampIsoToNow,
  isRecentlyUsed,
  RECENT_USE_WARNING_WINDOW_MS,
} from "./APIKeysTabPanel";
import type { MemberAPIKey } from "@/lib/api/member-credentials";

// ---------- Mocks ------------------------------------------------------------

const mockGetMemberCredentials = vi.fn();
const mockCreateAPIKey = vi.fn();
vi.mock("@/lib/api/member-credentials", () => ({
  getMemberCredentials: (...a: unknown[]) => mockGetMemberCredentials(...a),
  hideAPIKey: vi.fn(),
  regenerateAPIKey: vi.fn(),
  deleteWorkspaceMemberAPIKey: vi.fn(),
  deleteWorkspaceMemberAPIKeyById: vi.fn(),
  createAPIKey: (...a: unknown[]) => mockCreateAPIKey(...a),
}));

const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
}));

// #2129: per-key default contexts come from the user's key list.
const mockGetAPIKeys = vi.fn();
const mockUpdateDefault = vi.fn();
vi.mock("@/lib/api/api-keys", () => ({
  getAPIKeys: (...a: unknown[]) => mockGetAPIKeys(...a),
  updateAPIKeyDefaultContext: (...a: unknown[]) => mockUpdateDefault(...a),
}));

// Key-as-text translator so column headers assert by their i18n key.
vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "ws-1",
    currentWorkspace: { id: "ws-1", current_user_role: "owner" },
  }),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1", timezone: "UTC" } }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/hooks/useCopyFeedback", () => ({
  useCopyFeedback: () => ({ isCopied: () => false, copyToTarget: vi.fn() }),
}));

vi.mock("@/hooks/useAutoOpenOnFreshWindow", () => ({
  useAutoOpenOnFreshWindow: () => [false, vi.fn()],
}));

// MCPConfigBlock is unrelated to the table behavior under test.
vi.mock("@/components/credentials/MCPConfigBlock", () => ({
  MCPConfigBlock: () => null,
}));

// ---------- Helpers ----------------------------------------------------------

function makeKey(overrides: Partial<MemberAPIKey> = {}): MemberAPIKey {
  return {
    id: "akey_0000000000000000000001",
    name: "prod-api",
    key_prefix: "kagura_abc123",
    plaintext_key: null,
    is_visible: false,
    visibility_expires_at: null,
    created_at: "2026-04-01T00:00:00Z",
    last_used_at: null,
    revoked_at: null,
    bound_context_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetContexts.mockResolvedValue({ contexts: [], total: 0 });
  mockGetAPIKeys.mockResolvedValue([]);
});

describe("APIKeysTabPanel — table view (#943)", () => {
  it("renders the API keys as a table with the expected column headers", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    const headers = within(table);
    expect(headers.getByText("colName")).toBeInTheDocument();
    expect(headers.getByText("colKey")).toBeInTheDocument();
    expect(headers.getByText("colStatus")).toBeInTheDocument();
    expect(headers.getByText("colLastUsed")).toBeInTheDocument();
    expect(headers.getByText("colCreated")).toBeInTheDocument();
    expect(headers.getByText("colActions")).toBeInTheDocument();
  });

  it("shows '—' in Last used for a key that has never authenticated", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey({ id: "akey_0000000000000000000007", name: "never-used", last_used_at: null })],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    // The Last used em-dash carries the "neverUsed" label (the Default
    // context column renders its own "—", #2129).
    expect(within(table).getByLabelText("neverUsed")).toHaveTextContent("—");
  });

  it("shows a value (not '—') in Last used for a key that has authenticated", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          id: "akey_0000000000000000000009",
          name: "used-key",
          last_used_at: "2026-06-01T00:00:00Z",
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    await waitFor(() =>
      expect(within(table).getByText("used-key")).toBeInTheDocument(),
    );
    // A populated last_used_at must not collapse to the em-dash placeholder.
    expect(within(table).queryByLabelText("neverUsed")).not.toBeInTheDocument();
  });
});

describe("APIKeysTabPanel — role badge + provenance (key clarity Phase 1)", () => {
  it("shows the manage badge on keys when the viewer is admin/owner", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("roleBadgeManage")).toBeInTheDocument();
    expect(within(table).queryByText("roleBadgeData")).not.toBeInTheDocument();
  });

  it("shows the data-only badge when the viewer is a member", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "member",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("roleBadgeData")).toBeInTheDocument();
    expect(
      within(table).queryByText("roleBadgeManage"),
    ).not.toBeInTheDocument();
  });

  it("shows the public-bind badge INSTEAD of the role badge on bound keys", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey({ bound_context_id: "ctx-1" })],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("publicBindBadge")).toBeInTheDocument();
    expect(
      within(table).queryByText("roleBadgeManage"),
    ).not.toBeInTheDocument();
  });

  it("marks the admin-cli key with the setup provenance hint", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey({ name: "admin-cli" })],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("setupKeyHint")).toBeInTheDocument();
  });

  it("renders the role help line matching the viewer role", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "viewer",
    });

    render(<APIKeysTabPanel />);

    expect(await screen.findByText("roleHelpData")).toBeInTheDocument();
    expect(screen.queryByText("roleHelpManage")).not.toBeInTheDocument();
  });
});

describe("isRecentlyUsed (key clarity Phase 1)", () => {
  it("is false for a never-used key", () => {
    expect(isRecentlyUsed(null)).toBe(false);
  });

  it("is true for a key used one hour ago", () => {
    const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    expect(isRecentlyUsed(oneHourAgo)).toBe(true);
  });

  it("is false for a key used 25 hours ago", () => {
    const twentyFiveHoursAgo = new Date(
      Date.now() - RECENT_USE_WARNING_WINDOW_MS - 60 * 60 * 1000,
    ).toISOString();
    expect(isRecentlyUsed(twentyFiveHoursAgo)).toBe(false);
  });

  it("is true for a slightly-future timestamp (clock skew fail-safe)", () => {
    const oneMinuteAhead = new Date(Date.now() + 60 * 1000).toISOString();
    expect(isRecentlyUsed(oneMinuteAhead)).toBe(true);
  });
});

describe("clampIsoToNow (clock-skew display guard)", () => {
  it("returns past timestamps unchanged", () => {
    const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    expect(clampIsoToNow(oneHourAgo)).toBe(oneHourAgo);
  });

  it("clamps future timestamps to now so the warning never reads 'in X'", () => {
    const oneMinuteAhead = new Date(Date.now() + 60 * 1000).toISOString();
    const clamped = clampIsoToNow(oneMinuteAhead);
    expect(new Date(clamped).getTime()).toBeLessThanOrEqual(Date.now());
  });
});

describe("APIKeysTabPanel — recent-use warning on destructive dialogs", () => {
  it("shows the warning in the regenerate dialog for a recently used key", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          last_used_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);
    await screen.findByRole("table");

    fireEvent.click(screen.getAllByText("regenerate")[0]);

    expect(await screen.findByText("recentUseWarning")).toBeInTheDocument();
  });

  it("does NOT show the warning for a key last used 3 days ago", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          last_used_at: new Date(
            Date.now() - 3 * 24 * 60 * 60 * 1000,
          ).toISOString(),
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);
    await screen.findByRole("table");

    fireEvent.click(screen.getAllByText("regenerate")[0]);

    expect(
      await screen.findByText("regenerateApiKeyTitle"),
    ).toBeInTheDocument();
    expect(screen.queryByText("recentUseWarning")).not.toBeInTheDocument();
  });

  it("shows the warning in the delete dialog for a recently used key", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          last_used_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);
    await screen.findByRole("table");

    fireEvent.click(screen.getAllByText("delete")[0]);

    expect(await screen.findByText("recentUseWarning")).toBeInTheDocument();
  });

  it("shows the warning in the public-bound revoke dialog for a recently used bound key", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          bound_context_id: "ctx-1",
          last_used_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);
    await screen.findByRole("table");

    fireEvent.click(screen.getAllByLabelText("publicBindRevoke")[0]);

    expect(await screen.findByText("recentUseWarning")).toBeInTheDocument();
  });
});

// ---------- #2129: MCP default context ---------------------------------------

const CTX_A = "11111111-1111-4111-8111-111111111111";
const CTX_B = "22222222-2222-4222-8222-222222222222";

function workspaceContexts() {
  return {
    contexts: [
      { id: CTX_A, name: "alpha", display_name: "Alpha" },
      { id: CTX_B, name: "beta", display_name: null },
    ],
    total: 2,
  };
}

function adminKey(
  id: string,
  default_context_id: string | null,
  default_context_name: string | null,
) {
  return { id, default_context_id, default_context_name };
}

describe("APIKeysTabPanel — default context (#2129)", () => {
  it("adds a Default context column", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("colDefaultContext")).toBeInTheDocument();
  });

  it("shows the current default in the inline select and PATCHes a change", async () => {
    const key = makeKey();
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([adminKey(key.id, CTX_A, "alpha")]);
    mockUpdateDefault.mockResolvedValue(adminKey(key.id, CTX_B, "beta"));

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    const select = (await within(table).findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(CTX_A));
    await waitFor(() =>
      expect(within(select).getByText("beta")).toBeInTheDocument(),
    );

    fireEvent.change(select, { target: { value: CTX_B } });
    fireEvent.click(within(table).getByText("save"));

    await waitFor(() =>
      expect(mockUpdateDefault).toHaveBeenCalledWith(key.id, CTX_B),
    );
    await waitFor(() => expect(select.value).toBe(CTX_B));
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "defaultContextSaved" }),
    );
  });

  it("clears the default with PATCH null", async () => {
    const key = makeKey();
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([adminKey(key.id, CTX_A, "alpha")]);
    mockUpdateDefault.mockResolvedValue(adminKey(key.id, null, null));

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    fireEvent.click(await within(table).findByText("clear"));

    await waitFor(() =>
      expect(mockUpdateDefault).toHaveBeenCalledWith(key.id, null),
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "defaultContextCleared" }),
    );
  });

  it("reports a refused change with a destructive toast", async () => {
    const key = makeKey();
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([adminKey(key.id, null, null)]);
    mockUpdateDefault.mockRejectedValue(new Error("Context not found"));

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    const select = (await within(table).findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() =>
      expect(within(select).getByText("Alpha")).toBeInTheDocument(),
    );
    fireEvent.change(select, { target: { value: CTX_A } });
    fireEvent.click(within(table).getByText("save"));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description: "Context not found",
        }),
      ),
    );
    // The pending choice survives the failure.
    expect(select.value).toBe(CTX_A);
  });

  it.each([
    ["public-bound", { bound_context_id: CTX_A }],
    ["connector", { name: "connector:33333333-3333-4333-8333-333333333333" }],
  ])("offers no control for a %s key", async (_label, overrides) => {
    const key = makeKey(overrides);
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([adminKey(key.id, null, null)]);

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    await waitFor(() => expect(mockGetAPIKeys).toHaveBeenCalled());
    expect(
      within(table).queryByLabelText("defaultContextFor"),
    ).not.toBeInTheDocument();
    expect(within(table).getByText("none")).toBeInTheDocument();
  });

  it("offers no control when the key list failed to load", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [makeKey()],
      target_user_role: "owner",
    });
    mockGetAPIKeys.mockRejectedValue(new Error("boom"));

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    await waitFor(() => expect(mockGetAPIKeys).toHaveBeenCalled());
    expect(
      within(table).queryByLabelText("defaultContextFor"),
    ).not.toBeInTheDocument();
    expect(
      within(table).getByText("defaultContextLoadFailed"),
    ).toBeInTheDocument();
  });

  it("shows a read-only default by its display name", async () => {
    const key = makeKey({ revoked_at: "2026-10-01T00:00:00Z" });
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([adminKey(key.id, CTX_A, "alpha")]);

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(await within(table).findByText("Alpha")).toBeInTheDocument();
    expect(
      within(table).queryByLabelText("defaultContextFor"),
    ).not.toBeInTheDocument();
  });

  it("names an unreadable default instead of 'No default'", async () => {
    const key = makeKey();
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [key],
      target_user_role: "owner",
    });
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockGetAPIKeys.mockResolvedValue([
      adminKey(key.id, "44444444-4444-4444-8444-444444444444", null),
    ]);

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    const select = (await within(table).findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    await waitFor(() =>
      expect(select.value).toBe("44444444-4444-4444-8444-444444444444"),
    );
    expect(within(select).getByText("unavailable")).toBeInTheDocument();
  });

  describe("create dialog", () => {
    async function openCreateDialog() {
      mockGetMemberCredentials.mockResolvedValue({
        api_keys: [makeKey()],
        target_user_role: "owner",
      });
      mockGetContexts.mockResolvedValue(workspaceContexts());
      render(<APIKeysTabPanel />);
      await screen.findByRole("table");
      fireEvent.click(screen.getByText("createApiKey"));
      await screen.findByText("createApiKeyTitle");
      fireEvent.change(screen.getByLabelText("keyName"), {
        target: { value: "ci" },
      });
    }

    it("offers the workspace contexts with 'No default' first", async () => {
      await openCreateDialog();
      const select = screen.getByLabelText(
        "defaultContextLabel",
      ) as HTMLSelectElement;
      await waitFor(() => expect(select.options).toHaveLength(3));
      expect(select.options[0].value).toBe("");
      expect(select.options[0].text).toBe("noDefault");
      expect(select.options[1].text).toBe("Alpha");
      expect(select.options[2].text).toBe("beta");
    });

    it("sets the chosen default on the new key", async () => {
      mockCreateAPIKey.mockResolvedValue(
        makeKey({ id: "akey_0000000000000000000042", name: "ci" }),
      );
      mockUpdateDefault.mockResolvedValue(
        adminKey("akey_0000000000000000000042", CTX_A, "alpha"),
      );
      await openCreateDialog();
      const select = screen.getByLabelText("defaultContextLabel");
      await waitFor(() =>
        expect(within(select).getByText("Alpha")).toBeInTheDocument(),
      );
      fireEvent.change(select, { target: { value: CTX_A } });
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() =>
        expect(mockUpdateDefault).toHaveBeenCalledWith(
          "akey_0000000000000000000042",
          CTX_A,
        ),
      );
      expect(mockCreateAPIKey).toHaveBeenCalledWith("ws-1", "user-1", {
        name: "ci",
      });
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ description: "createSuccess" }),
      );
    });

    it("does not touch the default when none is chosen", async () => {
      mockCreateAPIKey.mockResolvedValue(makeKey({ name: "ci" }));
      await openCreateDialog();
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() => expect(mockCreateAPIKey).toHaveBeenCalled());
      expect(mockUpdateDefault).not.toHaveBeenCalled();
    });

    it("reports a key created without its default", async () => {
      mockCreateAPIKey.mockResolvedValue(
        makeKey({ id: "akey_0000000000000000000042", name: "ci" }),
      );
      mockUpdateDefault.mockRejectedValue(new Error("Context not found"));
      await openCreateDialog();
      const select = screen.getByLabelText("defaultContextLabel");
      await waitFor(() =>
        expect(within(select).getByText("Alpha")).toBeInTheDocument(),
      );
      fireEvent.change(select, { target: { value: CTX_A } });
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            variant: "destructive",
            description: "defaultContextCreateFailed",
          }),
        ),
      );
    });

    it("uses a generic message when setting the default fails without one", async () => {
      mockCreateAPIKey.mockResolvedValue(
        makeKey({ id: "akey_0000000000000000000042", name: "ci" }),
      );
      mockUpdateDefault.mockRejectedValue(new Error(""));
      await openCreateDialog();
      const select = screen.getByLabelText("defaultContextLabel");
      await waitFor(() =>
        expect(within(select).getByText("Alpha")).toBeInTheDocument(),
      );
      fireEvent.change(select, { target: { value: CTX_A } });
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            variant: "destructive",
            description: "defaultContextCreateFailedGeneric",
          }),
        ),
      );
    });

    it("hides the default picker while binding to a public context", async () => {
      await openCreateDialog();
      expect(screen.getByLabelText("defaultContextLabel")).toBeInTheDocument();
      fireEvent.click(screen.getByLabelText("publicBindToggleLabel"));
      expect(
        screen.queryByLabelText("defaultContextLabel"),
      ).not.toBeInTheDocument();
    });
  });
});
