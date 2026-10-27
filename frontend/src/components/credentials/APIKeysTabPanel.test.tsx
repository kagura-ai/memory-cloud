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
  canSetDefaultContext,
  clampIsoToNow,
  isRecentlyUsed,
  RECENT_USE_WARNING_WINDOW_MS,
} from "./APIKeysTabPanel";
import type { MemberAPIKey } from "@/lib/api/member-credentials";

// ---------- Mocks ------------------------------------------------------------

const mockGetMemberCredentials = vi.fn();
const mockCreateAPIKey = vi.fn();
const mockRegenerate = vi.fn();
vi.mock("@/lib/api/member-credentials", () => ({
  getMemberCredentials: (...a: unknown[]) => mockGetMemberCredentials(...a),
  hideAPIKey: vi.fn(),
  regenerateAPIKey: (...a: unknown[]) => mockRegenerate(...a),
  deleteWorkspaceMemberAPIKey: vi.fn(),
  deleteWorkspaceMemberAPIKeyById: vi.fn(),
  createAPIKey: (...a: unknown[]) => mockCreateAPIKey(...a),
}));

const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
}));

// #2129: a key's default context is changed through PATCH /config/api-keys
// (#2188: it is read from the member-credentials payload itself).
const mockUpdateDefault = vi.fn();
vi.mock("@/lib/api/api-keys", () => ({
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
    default_context_id: null,
    default_context_name: null,
    agent_id: null,
    connector_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetContexts.mockResolvedValue({ contexts: [], total: 0 });
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

  it("offers no Regenerate on a connector key, renamed or not (#2299)", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          name: "renamed",
          connector_id: "33333333-3333-4333-8333-333333333333",
        }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    await waitFor(() =>
      expect(within(table).getByText("renamed")).toBeInTheDocument(),
    );
    expect(within(table).queryByText("regenerate")).not.toBeInTheDocument();
  });

  it("keeps Regenerate on a look-alike name without the link (#2299)", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({ name: "connector:33333333-3333-4333-8333-333333333333" }),
      ],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(await within(table).findByText("regenerate")).toBeInTheDocument();
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

describe("canSetDefaultContext — connector keys by connector_id (#2299)", () => {
  const CONNECTOR = "33333333-3333-4333-8333-333333333333";

  it("offers the control on an ordinary key", () => {
    expect(canSetDefaultContext(makeKey())).toBe(true);
  });

  it("refuses a connector key, renamed or not", () => {
    expect(
      canSetDefaultContext(
        makeKey({ name: `connector:${CONNECTOR}`, connector_id: CONNECTOR }),
      ),
    ).toBe(false);
    expect(
      canSetDefaultContext(
        makeKey({ name: "renamed", connector_id: CONNECTOR }),
      ),
    ).toBe(false);
  });

  it("treats a look-alike name without the link as an ordinary key", () => {
    // The API lets such a key take a default (#1952) — so must the UI.
    expect(
      canSetDefaultContext(makeKey({ name: `connector:${CONNECTOR}` })),
    ).toBe(true);
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

/** The PATCH /config/api-keys/{id} response fields the panel reads. */
function patched(
  id: string,
  default_context_id: string | null,
  default_context_name: string | null,
) {
  return { id, default_context_id, default_context_name };
}

/** Seed the member-credentials list with one key. */
function seed(key: MemberAPIKey) {
  mockGetMemberCredentials.mockResolvedValue({
    api_keys: [key],
    target_user_role: "owner",
  });
}

describe("APIKeysTabPanel — default context (#2129)", () => {
  it("adds a Default context column", async () => {
    seed(makeKey());

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("colDefaultContext")).toBeInTheDocument();
  });

  it("shows the current default in the inline select and PATCHes a change", async () => {
    const key = makeKey({
      default_context_id: CTX_A,
      default_context_name: "alpha",
    });
    seed(key);
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockUpdateDefault.mockResolvedValue(patched(key.id, CTX_B, "beta"));

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
    // The list is updated from the PATCH response, not refetched.
    expect(mockGetMemberCredentials).toHaveBeenCalledTimes(1);
  });

  it("clears the default with PATCH null", async () => {
    const key = makeKey({
      default_context_id: CTX_A,
      default_context_name: "alpha",
    });
    seed(key);
    mockGetContexts.mockResolvedValue(workspaceContexts());
    mockUpdateDefault.mockResolvedValue(patched(key.id, null, null));

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    const select = (await within(table).findByLabelText(
      "defaultContextFor",
    )) as HTMLSelectElement;
    fireEvent.click(await within(table).findByText("clear"));

    await waitFor(() =>
      expect(mockUpdateDefault).toHaveBeenCalledWith(key.id, null),
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "defaultContextCleared" }),
    );
    await waitFor(() => expect(select.value).toBe(""));
  });

  it("reports a refused change with a destructive toast", async () => {
    const key = makeKey();
    seed(key);
    mockGetContexts.mockResolvedValue(workspaceContexts());
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
    [
      "connector",
      {
        name: "connector:33333333-3333-4333-8333-333333333333",
        connector_id: "33333333-3333-4333-8333-333333333333",
      },
    ],
    // #2299: a renamed connector key is still classified by its link.
    [
      "renamed connector",
      { name: "renamed", connector_id: "33333333-3333-4333-8333-333333333333" },
    ],
    ["agent-bound", { agent_id: "55555555-5555-4555-8555-555555555555" }],
  ])("offers no control for a %s key", async (_label, overrides) => {
    seed(makeKey(overrides));
    mockGetContexts.mockResolvedValue(workspaceContexts());

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(
      within(table).queryByLabelText("defaultContextFor"),
    ).not.toBeInTheDocument();
    expect(within(table).getByText("none")).toBeInTheDocument();
  });

  it("shows a read-only default by its display name", async () => {
    seed(
      makeKey({
        revoked_at: "2026-10-01T00:00:00Z",
        default_context_id: CTX_A,
        default_context_name: "alpha",
      }),
    );
    mockGetContexts.mockResolvedValue(workspaceContexts());

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    expect(await within(table).findByText("Alpha")).toBeInTheDocument();
    expect(
      within(table).queryByLabelText("defaultContextFor"),
    ).not.toBeInTheDocument();
  });

  it("names an unreadable default instead of 'No default'", async () => {
    seed(
      makeKey({
        default_context_id: "44444444-4444-4444-8444-444444444444",
        default_context_name: null,
      }),
    );
    mockGetContexts.mockResolvedValue(workspaceContexts());

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
      seed(makeKey());
      mockGetContexts.mockResolvedValue(workspaceContexts());
      render(<APIKeysTabPanel />);
      await screen.findByRole("table");
      fireEvent.click(screen.getByText("createApiKey"));
      await screen.findByText("createApiKeyTitle");
      fireEvent.change(screen.getByLabelText("keyName"), {
        target: { value: "ci" },
      });
    }

    async function chooseDefault(contextId: string) {
      const select = screen.getByLabelText("defaultContextLabel");
      await waitFor(() =>
        expect(within(select).getByText("Alpha")).toBeInTheDocument(),
      );
      fireEvent.change(select, { target: { value: contextId } });
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

    it("sends the chosen default with the create call", async () => {
      mockCreateAPIKey.mockResolvedValue(
        makeKey({
          id: "akey_0000000000000000000042",
          name: "ci",
          default_context_id: CTX_A,
          default_context_name: "alpha",
        }),
      );
      await openCreateDialog();
      await chooseDefault(CTX_A);
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() =>
        expect(mockCreateAPIKey).toHaveBeenCalledWith("ws-1", "user-1", {
          name: "ci",
          default_context_id: CTX_A,
        }),
      );
      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({ description: "createSuccess" }),
        ),
      );
      // One call: no follow-up PATCH.
      expect(mockUpdateDefault).not.toHaveBeenCalled();
    });

    it("sends no default when none is chosen", async () => {
      mockCreateAPIKey.mockResolvedValue(makeKey({ name: "ci" }));
      await openCreateDialog();
      fireEvent.click(screen.getByText("createKey"));

      await waitFor(() =>
        expect(mockCreateAPIKey).toHaveBeenCalledWith("ws-1", "user-1", {
          name: "ci",
        }),
      );
      expect(mockUpdateDefault).not.toHaveBeenCalled();
    });

    it("shows a refused default in the dialog and creates nothing", async () => {
      mockCreateAPIKey.mockRejectedValue(new Error("Context not found"));
      await openCreateDialog();
      await chooseDefault(CTX_A);
      fireEvent.click(screen.getByText("createKey"));

      expect(await screen.findByText("Context not found")).toBeInTheDocument();
      // The dialog stays open; the list is not reloaded.
      expect(screen.getByText("createApiKeyTitle")).toBeInTheDocument();
      expect(mockGetMemberCredentials).toHaveBeenCalledTimes(1);
      expect(mockUpdateDefault).not.toHaveBeenCalled();
      expect(mockToast).not.toHaveBeenCalledWith(
        expect.objectContaining({ description: "createSuccess" }),
      );
    });

    it("mints one key when Create is clicked twice", async () => {
      let resolve: (k: MemberAPIKey) => void = () => {};
      mockCreateAPIKey.mockReturnValue(
        new Promise<MemberAPIKey>((r) => {
          resolve = r;
        }),
      );
      await openCreateDialog();
      fireEvent.click(screen.getByText("createKey"));
      fireEvent.click(screen.getByText("createKey"));
      resolve(makeKey({ name: "ci" }));

      await waitFor(() =>
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({ description: "createSuccess" }),
        ),
      );
      expect(mockCreateAPIKey).toHaveBeenCalledTimes(1);
    });

    it("starts fresh after closing on an error", async () => {
      mockCreateAPIKey.mockRejectedValue(new Error("Context not found"));
      await openCreateDialog();
      fireEvent.click(screen.getByText("createKey"));
      expect(await screen.findByText("Context not found")).toBeInTheDocument();

      fireEvent.keyDown(screen.getByText("createApiKeyTitle"), {
        key: "Escape",
      });
      await waitFor(() =>
        expect(screen.queryByText("createApiKeyTitle")).not.toBeInTheDocument(),
      );
      fireEvent.click(screen.getByText("createApiKey"));
      await screen.findByText("createApiKeyTitle");
      expect(screen.queryByText("Context not found")).not.toBeInTheDocument();
      expect(
        (screen.getByLabelText("keyName") as HTMLInputElement).value,
      ).toBe("");
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
