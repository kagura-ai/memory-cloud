/**
 * Tests for the Workspace > Secrets management page (#1134).
 *
 * Covers:
 *   - role gating: member/viewer → notAuthorized, no API calls;
 *   - admin/owner → loads secrets + recipient keys; the zero-knowledge note
 *     is always surfaced;
 *   - owner-only key actions: admin sees the owner-only note and no approve
 *     button; owner sees + can drive approve;
 *   - audit-chain verify wires to the API and renders the result.
 *
 * The page never imports a fetch/put client (no plaintext/ciphertext path), so
 * there is nothing to assert "is hidden" — the invariant holds by construction.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import WorkspaceSecretsPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableTranslator,
  useLocale: () => "en",
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatDateTime: (iso: string) => iso,
}));

let mockRole: string | undefined = "owner";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "ws-1",
    currentWorkspace: { current_user_role: mockRole },
  }),
}));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "UTC" } }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const api = vi.hoisted(() => ({
  listSecrets: vi.fn(),
  listSecretPubkeys: vi.fn(),
  approveSecretPubkey: vi.fn(),
  revokeSecretPubkey: vi.fn(),
  revokeSecretGrant: vi.fn(),
  verifySecretAudit: vi.fn(),
}));
vi.mock("@/lib/api/secrets", () => api);

const SECRETS = [
  {
    name: "cloudflare/api-token",
    status: "active",
    rotation_needed: false,
    current_version: 2,
    grant_count: 1,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: null,
  },
];
const PUBKEYS = [
  {
    id: "pk-pending",
    identity_id: "member-1",
    pubkey: "age1pendingxxx",
    fingerprint: "fp-pending-abc",
    label: "laptop",
    status: "pending",
    created_at: "2026-06-01T00:00:00Z",
    attested_at: null,
    revoked_at: null,
  },
  {
    id: "pk-active",
    identity_id: "member-2",
    pubkey: "age1activexxx",
    fingerprint: "fp-active-def",
    label: "ci",
    status: "active",
    created_at: "2026-06-01T00:00:00Z",
    attested_at: "2026-06-02T00:00:00Z",
    revoked_at: null,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockRole = "owner";
  api.listSecrets.mockResolvedValue(SECRETS);
  api.listSecretPubkeys.mockResolvedValue(PUBKEYS);
  api.approveSecretPubkey.mockResolvedValue(PUBKEYS[0]);
  api.verifySecretAudit.mockResolvedValue({
    valid: true,
    entries: 5,
    head: "abcdef0123456789",
    broken_at: null,
    reason: null,
  });
});

// ---------- Tests ------------------------------------------------------------

describe("WorkspaceSecretsPage (#1134)", () => {
  it("blocks non-admins and makes no API calls", async () => {
    mockRole = "member";
    render(<WorkspaceSecretsPage />);
    expect(
      await screen.findByText("secretStore.notAuthorized"),
    ).toBeInTheDocument();
    expect(api.listSecrets).not.toHaveBeenCalled();
    expect(api.listSecretPubkeys).not.toHaveBeenCalled();
  });

  it("loads secrets + recipient keys and always shows the zero-knowledge note", async () => {
    render(<WorkspaceSecretsPage />);
    expect(
      await screen.findByText("secretStore.zeroKnowledgeNote"),
    ).toBeInTheDocument();
    expect(await screen.findByText("cloudflare/api-token")).toBeInTheDocument();
    expect(screen.getByText("fp-pending-abc")).toBeInTheDocument();
    expect(api.listSecrets).toHaveBeenCalledTimes(1);
    expect(api.listSecretPubkeys).toHaveBeenCalledTimes(1);
  });

  it("hides key approve/revoke from a non-owner admin and shows the owner-only note", async () => {
    mockRole = "admin";
    render(<WorkspaceSecretsPage />);
    expect(
      await screen.findByText("secretStore.ownerOnlyKeyActions"),
    ).toBeInTheDocument();
    expect(screen.queryByText("secretStore.approve")).toBeNull();
  });

  it("lets an owner approve a pending recipient key", async () => {
    render(<WorkspaceSecretsPage />);
    // Row approve button opens the confirm dialog.
    fireEvent.click(await screen.findByText("secretStore.approve"));
    expect(
      await screen.findByText("secretStore.approveTitle"),
    ).toBeInTheDocument();
    // Confirm: the dialog's action button is the last "approve" in the DOM.
    const approveButtons = screen.getAllByText("secretStore.approve");
    fireEvent.click(approveButtons[approveButtons.length - 1]);
    await waitFor(() =>
      expect(api.approveSecretPubkey).toHaveBeenCalledWith("pk-pending"),
    );
  });

  it("runs an audit verification and renders the valid result", async () => {
    render(<WorkspaceSecretsPage />);
    fireEvent.click(await screen.findByText("secretStore.verify"));
    await waitFor(() => expect(api.verifySecretAudit).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("secretStore.auditValid"),
    ).toBeInTheDocument();
  });
});
