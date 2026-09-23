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
import {
  act,
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";

import ContextsPage from "./page";
import { ApiError } from "@/lib/api/base";
import { createContext } from "@/lib/api/contexts";
import { normalizeGate } from "@/lib/gates/featureGates";

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
  usePathname: () => "/workspace/contexts",
}));

// Stable translator — passing `createFirstContextNonAdmin` through as-is so
// assertions can match on the key. ICU params, when a call passes any, are
// echoed after the key so their flow is assertable (#1644).
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns?: string) => (k: string, params?: Record<string, unknown>) =>
      params ? `${k} ${JSON.stringify(params)}` : k,
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

const mockUseMemoryContext = vi.fn();
vi.mock("@/contexts/MemoryContextContext", () => ({
  useMemoryContext: () => mockUseMemoryContext(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// #1167: the OpenAI key probe is gated on features.byok; default on.
let mockFeatures: Record<string, boolean> | null = { byok: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
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
  mockUseMemoryContext.mockReset();
  mockUseMemoryContext.mockReturnValue({
    currentContext: null,
    contextId: null,
    contextName: null,
    isLoading: false,
    error: null,
    refresh: vi.fn(),
  });
  mockGetContexts.mockReset();
  mockCheckOpenAIKeyStatus.mockReset();
  mockGetEmbeddingModels.mockReset();
  mockToast.mockReset();
  mockPush.mockReset();
  mockReplace.mockReset();
  mockFeatures = { byok: true };
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ContextsPage BYOK gating (#1167)", () => {
  it("skips the OpenAI key probe when byok is off — no setup-needed block", async () => {
    mockFeatures = { byok: false };
    setupWithRole("owner");
    render(<ContextsPage />);

    // Neutral empty state (blue), not the amber "key required" gate — env
    // keys serve embeddings in a BYOK-off deployment.
    await waitFor(() =>
      expect(screen.getByText("noContextsYet")).toBeInTheDocument(),
    );
    expect(mockCheckOpenAIKeyStatus).not.toHaveBeenCalled();
    expect(screen.queryByText("setupNeededOpenAI")).toBeNull();
  });

  it("still probes the key when byok is on", async () => {
    setupWithRole("owner");
    render(<ContextsPage />);
    await waitFor(() => expect(mockCheckOpenAIKeyStatus).toHaveBeenCalled());
  });
});

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
        sleep_mode: "skip",
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

// ---------- Issue #559 + #561: sleep_mode badge + current marker/switch ----

function setupWithThreeContextsAndCurrent(currentId: string | null) {
  mockUseAuth.mockReturnValue({
    user: { current_workspace_id: WORKSPACE_ID },
    refetchUser: vi.fn(),
  });
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: {
      id: WORKSPACE_ID,
      plan_name: "pro",
      current_user_role: "owner",
    },
  });
  mockUseMemoryContext.mockReturnValue({
    currentContext: null,
    contextId: currentId,
    contextName: null,
    isLoading: false,
    error: null,
    refresh: vi.fn(),
  });
  mockCheckOpenAIKeyStatus.mockResolvedValue({ has_key: true });
  mockGetEmbeddingModels.mockResolvedValue({
    models: [],
    default_model: "small",
  });
  const baseFields = {
    description: "",
    memory_count: 0,
    last_activity_at: null,
    is_default: false,
    is_locked: false,
    is_private: true,
    is_public: false,
    embedding_model: "small",
    resource_id: null,
  };
  mockGetContexts.mockResolvedValue({
    contexts: [
      {
        ...baseFields,
        id: "ctx-full",
        name: "ctx-full",
        display_name: "Full Context",
        sleep_mode: "full",
      },
      {
        ...baseFields,
        id: "ctx-edges",
        name: "ctx-edges",
        display_name: "Edges Context",
        sleep_mode: "edges_only",
      },
      {
        ...baseFields,
        id: "ctx-skip",
        name: "ctx-skip",
        display_name: "Skip Context",
        sleep_mode: "skip",
      },
    ],
  });
}

describe("ContextsPage sleep_mode badge (#559)", () => {
  it("renders one SleepModeBadge per row covering all three modes", async () => {
    setupWithThreeContextsAndCurrent(null);
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("Full Context")).toBeInTheDocument(),
    );

    // Each row carries the i18n key for its sleep_mode badge label.
    expect(screen.getByLabelText("sleepModeBadgeFull")).toBeInTheDocument();
    expect(
      screen.getByLabelText("sleepModeBadgeEdgesOnly"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("sleepModeBadgeSkip")).toBeInTheDocument();
  });

  it("renders the Sleep column header in the table", async () => {
    setupWithThreeContextsAndCurrent(null);
    render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("Full Context")).toBeInTheDocument(),
    );

    expect(screen.getByText("sleepModeBadgeHeader")).toBeInTheDocument();
  });
});

describe("ContextsPage current marker (#561)", () => {
  it("marks only the current context row with aria-current and CurrentContextBadge", async () => {
    setupWithThreeContextsAndCurrent("ctx-edges");
    const { container } = render(<ContextsPage />);

    await waitFor(() =>
      expect(screen.getByText("Edges Context")).toBeInTheDocument(),
    );

    const currentRows = container.querySelectorAll('tr[aria-current="true"]');
    expect(currentRows.length).toBe(1);

    // CurrentContextBadge uses the "current" key as its aria-label and text.
    // Multiple matches are possible since the i18n key passes through verbatim,
    // but exactly one Current badge should be inside an aria-current row.
    const badge = currentRows[0].querySelector('[aria-label="current"]');
    expect(badge).not.toBeNull();
  });
});

// ---------- Issue #1643: quota upsells stop at the Plan page's own gate -----

/**
 * The banner and the dialog both offered a route to `/workspace/settings/plan`
 * unconditionally. That page is behind the `plan_page` deployment flag and is
 * owner-only, so on a default self-hosted deployment both dead-ended.
 *
 * `mockFeatures` here defaults to `{ byok: true }` — `plan_page` absent, which
 * is the OSS truth — so a case that wants the CTA opts in explicitly.
 */
describe("ContextsPage quota upsells behind the plan_page gate (#1643)", () => {
  /** At the cap with nothing visible: banner shown AND the empty state renders. */
  function setupAtCap(role: Role = "owner") {
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: "pro",
        current_user_role: role,
        max_contexts: 20,
        context_count: 20,
      },
    });
    mockGetContexts.mockResolvedValue({ contexts: [] });
    mockCheckOpenAIKeyStatus.mockResolvedValue({
      has_key: true,
      embedding_available: true,
    });
    mockGetEmbeddingModels.mockResolvedValue({
      models: [],
      default_model: "small",
    });
  }

  /** The empty-state Create button routes to the quota dialog at the cap. */
  async function openQuotaDialog() {
    const create = await screen.findByRole("button", { name: /^create$/i });
    fireEvent.click(create);
    // The dialog's own explanation — it renders in every case below.
    expect(await screen.findByText("quotaDialogTitle")).toBeInTheDocument();
    expect(screen.getByText("quotaDialogDescription")).toBeInTheDocument();
  }

  it("quota banner: explanation renders, plan link withheld when plan_page is off", async () => {
    setupAtCap();
    render(<ContextsPage />);

    // The banner div mixes an emoji, the sentence and (when shown) the link,
    // so match the substring — this is the shape create-gate.test.tsx uses.
    const banner = await screen.findByText(/quotaReachedDetail/);
    expect(screen.queryByText("quotaReachedPlansLink")).toBeNull();
    // The separating space moved inside the guard, so nothing dangles.
    const text = banner.textContent ?? "";
    expect(text).toBe(text.trimEnd());
  });

  it("quota banner: owner on a plan_page deployment gets the View plans link", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap();
    render(<ContextsPage />);

    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "quotaReachedPlansLink" }),
    ).toHaveAttribute("href", "/workspace/settings/plan");
  });

  it("quota banner: a non-owner gets no plan link even with plan_page on", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap("admin");
    render(<ContextsPage />);

    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
    expect(screen.queryByText("quotaReachedPlansLink")).toBeNull();
  });

  it("quota banner: no plan link while /system/info is unresolved", async () => {
    mockFeatures = null;
    setupAtCap();
    render(<ContextsPage />);

    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
    expect(screen.queryByText("quotaReachedPlansLink")).toBeNull();
  });

  it("quota dialog: footer shows only a Close control when plan_page is off", async () => {
    setupAtCap();
    render(<ContextsPage />);
    await openQuotaDialog();

    // The prose that IS the CTA goes with the button — leaving it would tell
    // the reader to visit a page this deployment does not have.
    expect(screen.queryByText("quotaDialogUpgradeHeading")).toBeNull();
    expect(screen.queryByText("quotaDialogUpgradeBody")).toBeNull();
    expect(screen.queryByRole("button", { name: "viewPlans" })).toBeNull();
    // A footer whose only control is "Cancel" reads wrong once there is
    // nothing to cancel.
    expect(screen.getByRole("button", { name: "close" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "cancel" })).toBeNull();
  });

  it("quota dialog: owner on a plan_page deployment gets View plans", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap();
    render(<ContextsPage />);
    await openQuotaDialog();

    expect(screen.getByText("quotaDialogUpgradeHeading")).toBeInTheDocument();
    expect(screen.getByText("quotaDialogUpgradeBody")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "viewPlans" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "cancel" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "close" })).toBeNull();
  });

  it("quota dialog: a non-owner gets the explanation and a Close control only", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap("admin");
    render(<ContextsPage />);
    await openQuotaDialog();

    expect(screen.queryByText("quotaDialogUpgradeHeading")).toBeNull();
    expect(screen.queryByRole("button", { name: "viewPlans" })).toBeNull();
    expect(screen.getByRole("button", { name: "close" })).toBeInTheDocument();
  });

  it("quota dialog: no CTA while /system/info is unresolved", async () => {
    mockFeatures = null;
    setupAtCap();
    render(<ContextsPage />);
    await openQuotaDialog();

    expect(screen.queryByText("quotaDialogUpgradeHeading")).toBeNull();
    expect(screen.queryByRole("button", { name: "viewPlans" })).toBeNull();
    expect(screen.getByRole("button", { name: "close" })).toBeInTheDocument();
  });
});

describe("ContextsPage create errors read the context cap from err.gate (#1644)", () => {
  const SERVER_MESSAGE =
    "Context limit reached. Your S plan allows 1 context(s) per workspace. Upgrade to M plan for more contexts.";

  /** An ApiError exactly as lib/api/base.ts would build it from this body. */
  function capRefusal(details: Record<string, unknown> | undefined): ApiError {
    return new ApiError({
      error: "QUOTA-001",
      message: SERVER_MESSAGE,
      status: 429,
      details,
      gate: normalizeGate(429, "QUOTA-001", details),
    });
  }

  const CURRENT_SERVER_BODY = {
    gate: "quota",
    quota_type: "contexts",
    current: 1,
    limit: 1,
    required_plan: "basic",
    required_plan_display: "M",
    current_plan: "free",
    feature: null,
    resets_at: null,
    addon_bonus: 0,
    requested: 1,
  };

  /** Owner, empty list: the empty-state Create opens the advanced dialog. */
  async function submitAdvancedCreate() {
    setupWithRole("owner");
    render(<ContextsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^create$/i }));
    fireEvent.change(
      await screen.findByPlaceholderText("contextNamePlaceholder"),
      { target: { value: "my-context" } },
    );
    const dialog = await screen.findByRole("dialog");
    const buttons = Array.from(dialog.querySelectorAll("button")).filter(
      (b) => b.textContent === "create",
    );
    expect(buttons).toHaveLength(1);
    // Inside act so the rejected create and its `finally` settle first.
    await act(async () => {
      fireEvent.click(buttons[0]);
    });
  }

  it("localizes the cap with the CURRENT tier's label and the limit from err.gate", async () => {
    vi.mocked(createContext).mockRejectedValueOnce(
      capRefusal(CURRENT_SERVER_BODY),
    );
    await submitAdvancedCreate();

    // `free` resolves to this deployment's label (S by default); the prose's
    // own "Your S plan" is never parsed.
    expect(
      await screen.findByText('contextLimitReached {"plan":"S","limit":1}'),
    ).toBeInTheDocument();
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
  });

  it("does not parse the server prose: a refusal without gate details shows the server text", async () => {
    // A server predating #1644 raised the context cap with no details at
    // all. The old regex pair turned its prose into the localized sentence;
    // nothing reads the prose now.
    vi.mocked(createContext).mockRejectedValueOnce(capRefusal(undefined));
    await submitAdvancedCreate();

    expect(await screen.findByText(SERVER_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/contextLimitReached/)).toBeNull();
  });

  it("does not render another quota as the context cap", async () => {
    vi.mocked(createContext).mockRejectedValueOnce(
      new ApiError({
        error: "QUOTA-001",
        message: "REST API daily quota exceeded",
        status: 429,
        gate: { state: "quota", quotaType: "api_rest_daily" },
      }),
    );
    await submitAdvancedCreate();

    expect(
      await screen.findByText("REST API daily quota exceeded"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/contextLimitReached/)).toBeNull();
  });
});
