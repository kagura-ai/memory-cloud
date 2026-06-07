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

import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { APIKeysTabPanel } from "./APIKeysTabPanel";
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
