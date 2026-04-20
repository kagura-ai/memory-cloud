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

  it("shows neither the Create button nor the non-admin message while currentWorkspace is null (full hydration)", async () => {
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

  it("shows neither the Create button nor the non-admin message during partial hydration (workspace present, role null)", async () => {
    // Partial hydration: currentWorkspace object is populated but
    // current_user_role has not resolved yet. Without the role-presence
    // guard, hasWorkspaceRole(null, "admin") returns false and the
    // non-admin message would flash for owner/admin users too.
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: "pro",
        current_user_role: null,
      },
    });
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

// ---------- Issue #398: New Context button + kebab role gating ---------------

function setupWithRoleAndOneContext(role: Role) {
  // Set up the auth + workspace mocks the same way setupWithRole does, but
  // skip its mockGetContexts call so we don't queue an empty-contexts response
  // that beats the populated one when the page calls getContexts() twice
  // (initial fetch + any post-action re-fetch).
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
  mockCheckOpenAIKeyStatus.mockResolvedValue({ has_key: true });
  mockGetEmbeddingModels.mockResolvedValue({
    models: [],
    default_model: "small",
  });
  mockGetContexts.mockResolvedValue({
    contexts: [
      {
        id: "ctx-1",
        name: "test-ctx",
        display_name: "Test Context",
        description: "",
        memory_count: 0,
        last_activity_at: null,
        is_default: false,
        is_locked: false,
        is_private: true,
        is_public: false,
        embedding_model: "small",
        resource_id: null,
      },
    ],
  });
}

describe("ContextsPage New Context header button (#398)", () => {
  // it.each spreads each row into the test-fn args. Pass scalars (not nested
  // arrays) so the role string isn't iterated character-by-character.
  it.each(["owner", "admin"] as const)(
    "renders the New Context button for %s",
    async (role) => {
      setupWithRole(role);
      render(<ContextsPage />);
      await waitFor(() =>
        expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
      );
      // The dropdown trigger button contains a Plus icon + "newContext" text.
      expect(
        screen.getByText("newContext", { exact: false }),
      ).toBeInTheDocument();
    },
  );

  it.each(["member", "viewer"] as const)(
    "hides the New Context button for %s",
    async (role) => {
      setupWithRole(role);
      render(<ContextsPage />);
      await waitFor(() =>
        expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
      );
      expect(screen.queryByText("newContext", { exact: false })).toBeNull();
    },
  );
});

describe("ContextsPage per-row kebab menu (#398)", () => {
  it.each(["owner", "admin"] as const)(
    "renders the kebab menu trigger for %s",
    async (role) => {
      setupWithRoleAndOneContext(role);
      const { container } = render(<ContextsPage />);
      await waitFor(() =>
        expect(screen.getByText("Test Context")).toBeInTheDocument(),
      );
      // Two dropdown triggers when admin/owner: New Context (header) + kebab (row).
      // Querying via aria-haspopup is independent of lucide icon class churn.
      const triggers = container.querySelectorAll(
        'button[aria-haspopup="menu"]',
      );
      expect(triggers.length).toBe(2);
    },
  );

  it.each(["member", "viewer"] as const)(
    "hides the kebab menu trigger for %s",
    async (role) => {
      setupWithRoleAndOneContext(role);
      const { container } = render(<ContextsPage />);
      await waitFor(() =>
        expect(screen.getByText("Test Context")).toBeInTheDocument(),
      );
      // No dropdown triggers anywhere — both the header New Context button
      // and the per-row kebab are gated behind the same admin check.
      expect(
        container.querySelectorAll('button[aria-haspopup="menu"]').length,
      ).toBe(0);
      // The BarChart "view usage" button stays visible — overview is reachable
      // by every role, so the navigation affordance must remain.
      expect(
        container.querySelector('button[title="viewUsage"]'),
      ).not.toBeNull();
    },
  );
});
