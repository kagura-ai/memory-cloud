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
  isRecentlyUsed,
  RECENT_USE_WARNING_WINDOW_MS,
} from "./APIKeysTabPanel";
import type { MemberAPIKey } from "@/lib/api/member-credentials";

// ---------- Mocks ------------------------------------------------------------

const mockGetMemberCredentials = vi.fn();
vi.mock("@/lib/api/member-credentials", () => ({
  getMemberCredentials: (...a: unknown[]) => mockGetMemberCredentials(...a),
  hideAPIKey: vi.fn(),
  regenerateAPIKey: vi.fn(),
  deleteWorkspaceMemberAPIKey: vi.fn(),
  deleteWorkspaceMemberAPIKeyById: vi.fn(),
  createAPIKey: vi.fn(),
}));

vi.mock("@/lib/api/contexts", () => ({
  getContexts: vi.fn().mockResolvedValue([]),
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

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
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
    id: 1,
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
      api_keys: [makeKey({ id: 7, name: "never-used", last_used_at: null })],
      target_user_role: "owner",
    });

    render(<APIKeysTabPanel />);

    const table = await screen.findByRole("table");
    // Exactly one em-dash inside the table → the single null-last_used key.
    expect(within(table).getByText("—")).toBeInTheDocument();
  });

  it("shows a value (not '—') in Last used for a key that has authenticated", async () => {
    mockGetMemberCredentials.mockResolvedValue({
      api_keys: [
        makeKey({
          id: 9,
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
    expect(within(table).queryByText("—")).not.toBeInTheDocument();
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
