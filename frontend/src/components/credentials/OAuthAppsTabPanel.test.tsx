/**
 * Tests for OAuthAppsTabPanel after #1836 (custom-only OAuth apps).
 *
 * Claude, ChatGPT, Cursor and Claude Code register themselves through
 * Dynamic Client Registration, so the tab no longer offers the "Create
 * Claude app" / "Create ChatGPT app" presets. Apps created through those
 * presets before this change are still listed (with a note that they are no
 * longer needed) so their owners can delete them.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OAuthAppsTabPanel } from "./OAuthAppsTabPanel";
import type { OAuth2Client } from "@/lib/api/oauth";

const mockGetOAuth2Clients = vi.fn();
vi.mock("@/lib/api/oauth", () => ({
  getOAuth2Clients: (...a: unknown[]) => mockGetOAuth2Clients(...a),
  createOAuth2Client: vi.fn(),
  deleteOAuth2Client: vi.fn(),
  regenerateOAuth2ClientSecret: vi.fn(),
}));

vi.mock("@/lib/api/member-credentials", () => ({
  hideOAuthClientSecret: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspaceId: "ws-1" }),
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

// The dialogs own their forms; here only the panel's layout is under test.
vi.mock("@/components/oauth/EditOAuthClientDialog", () => ({
  EditOAuthClientDialog: () => null,
}));
vi.mock("@/components/oauth/CreateCustomOAuthAppDialog", () => ({
  CreateCustomOAuthAppDialog: ({ isOpen }: { isOpen: boolean }) =>
    isOpen ? <div>custom-app-dialog</div> : null,
}));

function makeClient(overrides: Partial<OAuth2Client> = {}): OAuth2Client {
  return {
    client_id: "oauth_abc",
    client_name: "My Connector",
    redirect_uris: ["https://example.com/callback"],
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    scope: "openid memory:read memory:write",
    token_endpoint_auth_method: "client_secret_post",
    provider: "custom",
    created_at: "2026-04-01T00:00:00Z",
    plaintext_secret: null,
    is_visible: false,
    visibility_expires_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OAuthAppsTabPanel — custom-only (#1836)", () => {
  it("offers no Claude / ChatGPT preset creation and explains self-registration", async () => {
    mockGetOAuth2Clients.mockResolvedValue([]);

    render(<OAuthAppsTabPanel />);

    expect(await screen.findByText("dcrNote")).toBeInTheDocument();
    expect(screen.queryByText("createOAuthApp")).not.toBeInTheDocument();
    expect(screen.queryByText("noOAuthApp")).not.toBeInTheDocument();
    expect(screen.queryByText("claudeSubtitle")).not.toBeInTheDocument();
    expect(screen.queryByText("chatgptSubtitle")).not.toBeInTheDocument();
  });

  it("shows no MCP URL block — the connection card above the tabs owns it", async () => {
    mockGetOAuth2Clients.mockResolvedValue([]);

    render(<OAuthAppsTabPanel />);

    await screen.findByText("dcrNote");
    expect(screen.queryByText(/\/mcp/)).not.toBeInTheDocument();
    expect(screen.queryByText("mcpConnection")).not.toBeInTheDocument();
  });

  it("still lists a preset app created before this change, flagged as no longer needed, and lets its owner delete it", async () => {
    mockGetOAuth2Clients.mockResolvedValue([
      makeClient({
        client_id: "oauth_claude",
        client_name: "Claude",
        provider: "claude",
        redirect_uris: ["https://claude.ai/api/mcp/auth_callback"],
      }),
      makeClient({ client_id: "oauth_custom", client_name: "My Connector" }),
    ]);

    render(<OAuthAppsTabPanel />);

    expect(await screen.findByText("Claude")).toBeInTheDocument();
    expect(screen.getByText("My Connector")).toBeInTheDocument();
    // One legacy note for the preset app, none for the custom one.
    expect(screen.getAllByText("legacyPresetNote")).toHaveLength(1);

    fireEvent.click(screen.getAllByText("delete")[0]);

    expect(await screen.findByText("deleteOAuthTitle")).toBeInTheDocument();
  });

  it("opens the custom app dialog from the create button", async () => {
    mockGetOAuth2Clients.mockResolvedValue([]);

    render(<OAuthAppsTabPanel />);

    await screen.findByText("dcrNote");
    fireEvent.click(screen.getAllByText("createCustomApp")[0]);

    expect(await screen.findByText("custom-app-dialog")).toBeInTheDocument();
  });
});
