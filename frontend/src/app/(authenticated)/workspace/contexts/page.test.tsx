/**
 * Tests for the Contexts list page empty-state CTA role gating (Issue #382).
 *
 * Covers: empty-state Alert renders the "Create" button only for
 * owner/admin roles; member/viewer roles see the `createFirstContextNonAdmin`
 * informational message instead. Backend enforces owner/admin at
 * context_service.py:134-137 — this test guards the UI from showing a
 * broken CTA that would produce a 400.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import ContextsPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGetContexts = vi.fn();
const mockGetEmbeddingModels = vi.fn();
const mockCheckOpenAIKeyStatus = vi.fn();

vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...args: unknown[]) => mockGetContexts(...args),
  createContext: vi.fn(),
  getContextStats: vi.fn(),
  getContextSearchConfig: vi.fn(),
  updateContextSearchConfig: vi.fn(),
  getEmbeddingModels: (...args: unknown[]) => mockGetEmbeddingModels(...args),
}));

vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: (...args: unknown[]) =>
    mockCheckOpenAIKeyStatus(...args),
}));

vi.mock("@/lib/api/external-keys", () => ({
  createExternalAPIKey: vi.fn(),
}));

const mockPush = vi.fn();
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  useSearchParams: () => new URLSearchParams(),
}));

// Stable translator — passing `createFirstContextNonAdmin` through as-is so
// assertions can match on the key.
vi.mock("next-intl", () => ({
  useTranslations: (_ns?: string) => (k: string) => k,
  useLocale: () => "en",
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// ---------- Helpers ----------------------------------------------------------

type Role = "owner" | "admin" | "member" | "viewer";

const WORKSPACE_ID = "ws-1";

function setupWithRole(role: Role) {
  mockUseAuth.mockReturnValue({
    user: { current_workspace_id: WORKSPACE_ID },
    refetchUser: vi.fn(),
  });
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: {
      id: WORKSPACE_ID,
      plan_name: "pro",
      current_user_role: role,
    },
  });
  mockGetContexts.mockResolvedValue({ contexts: [] });
  mockCheckOpenAIKeyStatus.mockResolvedValue({ has_key: true });
  mockGetEmbeddingModels.mockResolvedValue({
    models: [],
    default_model: "small",
  });
}

beforeEach(() => {
  mockUseAuth.mockReset();
  mockUseWorkspace.mockReset();
  mockGetContexts.mockReset();
  mockCheckOpenAIKeyStatus.mockReset();
  mockGetEmbeddingModels.mockReset();
  mockToast.mockReset();
  mockPush.mockReset();
  mockReplace.mockReset();
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ContextsPage empty-state CTA role gating (#382)", () => {
  it("renders the Create button for owner", async () => {
    setupWithRole("owner");
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );

    expect(
      screen.getByRole("button", { name: /^create$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("createFirstContext")).toBeInTheDocument();
    expect(screen.queryByText("createFirstContextNonAdmin")).toBeNull();
  });

  it("renders the Create button for admin", async () => {
    setupWithRole("admin");
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );

    expect(
      screen.getByRole("button", { name: /^create$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("createFirstContext")).toBeInTheDocument();
    expect(screen.queryByText("createFirstContextNonAdmin")).toBeNull();
  });

  it("hides the Create button and shows the non-admin message for member", async () => {
    setupWithRole("member");
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );

    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.getByText("createFirstContextNonAdmin")).toBeInTheDocument();
    expect(screen.queryByText("createFirstContext")).toBeNull();
  });

  it("hides the Create button and shows the non-admin message for viewer", async () => {
    setupWithRole("viewer");
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );

    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.getByText("createFirstContextNonAdmin")).toBeInTheDocument();
    expect(screen.queryByText("createFirstContext")).toBeNull();
  });

  it("shows neither the Create button nor the non-admin message while currentWorkspace is hydrating", async () => {
    // During WorkspaceContext hydration, current_user_role is unknown. Rendering
    // the non-admin message would briefly mislead an owner/admin ("ask an
    // owner/admin"), and rendering the CTA would briefly mislead a
    // member/viewer. Render a neutral empty state until the role is known.
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({ currentWorkspace: null });
    mockGetContexts.mockResolvedValue({ contexts: [] });
    mockCheckOpenAIKeyStatus.mockResolvedValue({ has_key: true });
    mockGetEmbeddingModels.mockResolvedValue({
      models: [],
      default_model: "small",
    });

    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );

    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByText("createFirstContextNonAdmin")).toBeNull();
    expect(screen.queryByText("createFirstContext")).toBeNull();
  });
});
