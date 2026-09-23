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
import type { PlanTierFeature } from "@/lib/api/workspaces";
import {
  act,
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
  within,
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
// #1645: the shared-contexts gate and the context-cap descriptor read the
// shared tier matrix (`null` = still resolving). Default: the OSS matrix, so
// `plan_name` decides exactly as the tier's row does.
const OSS_TIERS = [
  { name: "free", display_name: "S", max_contexts: 1, shared_contexts: false },
  { name: "basic", display_name: "M", max_contexts: 3, shared_contexts: false },
  { name: "pro", display_name: "L", max_contexts: 20, shared_contexts: true },
  {
    name: "promax",
    display_name: "XL",
    max_contexts: 1000,
    shared_contexts: true,
  },
] as unknown as PlanTierFeature[];
let mockTiers: PlanTierFeature[] | null = OSS_TIERS;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
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
  mockTiers = OSS_TIERS;
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
 * The cap used to get two treatments — a yellow banner that linked to
 * `/workspace/settings/plan` and a quota dialog with a "View plans" action —
 * and both offered that route unconditionally. That page is behind the
 * `plan_page` deployment flag and is owner-only, so on a default self-hosted
 * deployment both dead-ended (#1643).
 *
 * #1646 (Q1): ONE inline gate notice replaces both. Its CTA follows the
 * descriptor's narrowed `canUpgrade`, so these cases now pin the notice.
 * Under this file's key-echo mock the notice renders `gate.*` keys relative
 * to `gate` ("quota.title", "quota.action", …) with their arguments.
 *
 * `mockFeatures` here defaults to `{ byok: true }` — `plan_page` absent, which
 * is the OSS truth — so a case that wants the CTA opts in explicitly.
 */
describe("ContextsPage quota upsells behind the plan_page gate (#1643)", () => {
  /** At the cap with nothing visible: notice shown AND the empty state renders. */
  function setupAtCap(role: Role = "owner", plan = "pro", cap = 20) {
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: plan,
        current_user_role: role,
        max_contexts: cap,
        context_count: cap,
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

  /** The cap notice (an Alert), found by its title. */
  async function findCapNotice(): Promise<HTMLElement> {
    const title = await screen.findByText(/^quota\.title/);
    const notice = title.closest('[role="alert"]');
    if (!(notice instanceof HTMLElement)) throw new Error("no cap notice");
    return notice;
  }

  const CTA = /^quota\.action/;

  it("quota notice: explanation renders, plan CTA withheld when plan_page is off", async () => {
    setupAtCap();
    render(<ContextsPage />);

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.description/);
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
    // Never promise an upgrade the reader cannot act on.
    expect(notice).not.toHaveTextContent(/quota\.upsell/);
  });

  it("quota notice: owner on a plan_page deployment gets the View plans CTA", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap();
    render(<ContextsPage />);

    const notice = await findCapNotice();
    // pro's cap is raised by promax (XL on this deployment).
    expect(notice).toHaveTextContent(/quota\.upsell \{"plan":"XL"/);
    fireEvent.click(screen.getByRole("button", { name: CTA }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("quota notice: a non-owner gets no plan CTA even with plan_page on", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap("admin");
    render(<ContextsPage />);

    await findCapNotice();
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
  });

  it("quota notice: no plan CTA while /system/info is unresolved", async () => {
    mockFeatures = null;
    setupAtCap();
    render(<ContextsPage />);

    await findCapNotice();
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
  });

  // #1645: the upsell reads the cap descriptor's NARROWED canUpgrade — an
  // owner whom no served tier can lift is not sent to the Plan page.
  it("quota notice: no plan CTA at the top tier's cap, even for an owner on plan_page (#1645)", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setupAtCap("owner", "promax", 1000);
    render(<ContextsPage />);

    const notice = await findCapNotice();
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
    expect(notice).not.toHaveTextContent(/quota\.upsell/);
  });

  it("quota notice: no upsell while the tier matrix is unresolved (#1645)", async () => {
    mockFeatures = { byok: true, plan_page: true };
    mockTiers = null;
    setupAtCap();
    render(<ContextsPage />);

    const notice = await findCapNotice();
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
    expect(notice).not.toHaveTextContent(/quota\.upsell/);
  });

  it("quota notice: a zero cap a served tier raises offers the upgrade (#1645)", async () => {
    // An operator tier that excludes contexts (cap 0): basic's cap is above
    // zero, so the Plan page does lift it.
    mockFeatures = { byok: true, plan_page: true };
    mockTiers = OSS_TIERS.map((tier) =>
      tier.name === "free" ? { ...tier, max_contexts: 0 } : tier,
    ) as PlanTierFeature[];
    setupAtCap("owner", "free", 0);
    render(<ContextsPage />);

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.upsell \{"plan":"M"/);
    expect(screen.getByRole("button", { name: CTA })).toBeInTheDocument();
  });

  it("quota notice: a zero cap no served tier raises explains itself with no upsell (#1645)", async () => {
    mockFeatures = { byok: true, plan_page: true };
    mockTiers = OSS_TIERS.map((tier) => ({
      ...tier,
      max_contexts: 0,
    })) as PlanTierFeature[];
    setupAtCap("owner", "free", 0);
    render(<ContextsPage />);

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.description/);
    expect(notice).not.toHaveTextContent(/quota\.upsell/);
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
  });
});

// ---------- #1646 Q1: one cap notice, and the create controls it explains ----

describe("ContextsPage context cap: one notice, disabled create controls (#1646)", () => {
  function setupCap(opts: {
    cap: number;
    count: number;
    plan?: string;
    role?: Role;
    canEmbed?: boolean;
    visible?: number;
  }) {
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: opts.plan ?? "pro",
        current_user_role: opts.role ?? "owner",
        max_contexts: opts.cap,
        context_count: opts.count,
      },
    });
    mockGetContexts.mockResolvedValue({
      contexts: Array.from({ length: opts.visible ?? 0 }, (_, i) => ({
        id: `c${i}`,
        name: `ctx-${i}`,
        memory_count: 0,
        sleep_mode: "full",
      })),
    });
    mockCheckOpenAIKeyStatus.mockResolvedValue({
      has_key: opts.canEmbed ?? true,
      embedding_available: opts.canEmbed ?? true,
    });
    mockGetEmbeddingModels.mockResolvedValue({
      models: [],
      default_model: "small",
    });
  }

  /** Radix opens a menu on a primary-button pointerdown on its trigger. */
  async function openNewContextMenu() {
    const trigger = await screen.findByRole("button", { name: /newContext/ });
    fireEvent.pointerDown(trigger, { button: 0, pointerType: "mouse" });
    return screen.findAllByRole("menuitem");
  }

  const NOTICE_ID = "context-quota-notice";

  it("one notice: the workspace's tier by its label, its cap and its usage — no emoji, no raw plan key", async () => {
    setupCap({ cap: 20, count: 20 });
    render(<ContextsPage />);

    const notice = await screen.findByText(/^quota\.title/);
    const alert = notice.closest('[role="alert"]');
    expect(alert).toHaveAttribute("id", NOTICE_ID);
    const description = screen.getByText(/^quota\.description /);
    expect(description.textContent).toContain('"currentPlan":"L"');
    expect(description.textContent).toContain('"limit":20');
    expect(description.textContent).toContain('"current":20');
    expect(document.body.textContent).not.toContain("⚠️");
    expect(description.textContent).not.toMatch(/"pro"/);
    // Exactly one treatment of the cap.
    expect(screen.getAllByText(/^quota\.title/)).toHaveLength(1);
  });

  it("the empty state's Create is disabled at the cap and described by the notice; no dialog opens", async () => {
    setupCap({ cap: 20, count: 20 });
    render(<ContextsPage />);

    const create = await screen.findByRole("button", { name: /^create$/i });
    expect(create).toBeDisabled();
    expect(create).toHaveAttribute("aria-describedby", NOTICE_ID);
    expect(create).toHaveAccessibleDescription(/quota\.title/);
    fireEvent.click(create);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("the no-embedding empty state's Create is disabled the same way", async () => {
    setupCap({ cap: 20, count: 20, canEmbed: false });
    render(<ContextsPage />);

    await screen.findByText("setupNeededOpenAI");
    const create = screen.getByRole("button", { name: /^create$/i });
    expect(create).toBeDisabled();
    expect(create).toHaveAttribute("aria-describedby", NOTICE_ID);
    fireEvent.click(create);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the header menu stays openable at the cap; both create items are disabled and described by the notice", async () => {
    setupCap({ cap: 20, count: 20, visible: 1 });
    render(<ContextsPage />);

    const trigger = await screen.findByRole("button", { name: /newContext/ });
    expect(trigger).not.toBeDisabled();
    const items = await openNewContextMenu();
    expect(items).toHaveLength(2);
    for (const item of items) {
      expect(item).toHaveAttribute("aria-disabled", "true");
      expect(item).toHaveAttribute("aria-describedby", NOTICE_ID);
      fireEvent.click(item);
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("below the cap: no notice, and the menu items open their dialogs", async () => {
    setupCap({ cap: 20, count: 3, visible: 1 });
    render(<ContextsPage />);

    const items = await openNewContextMenu();
    expect(screen.queryByText(/^quota\.title/)).toBeNull();
    for (const item of items) {
      expect(item).not.toHaveAttribute("aria-disabled");
      expect(item).not.toHaveAttribute("aria-describedby");
    }
    fireEvent.click(items[0]);
    expect(await screen.findByText("quickCreateContext")).toBeInTheDocument();
  });

  it("below the cap: the empty state's Create opens the advanced dialog", async () => {
    setupCap({ cap: 20, count: 0 });
    render(<ContextsPage />);

    const create = await screen.findByRole("button", { name: /^create$/i });
    expect(create).not.toBeDisabled();
    expect(create).not.toHaveAttribute("aria-describedby");
    fireEvent.click(create);
    expect(await screen.findByText("createDialogTitle")).toBeInTheDocument();
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

  /**
   * #1646 (Q2): a quota refusal renders as the gate notice inside the dialog,
   * not as a sentence. Under this file's key-echo mock its lines are the
   * `gate.quota.*` keys followed by their arguments.
   */
  async function findRefusalNotice(): Promise<HTMLElement> {
    const dialog = await screen.findByRole("dialog");
    const title = await within(dialog).findByText(/^quota\.title /);
    const notice = title.closest('[role="alert"]');
    if (!(notice instanceof HTMLElement)) throw new Error("no refusal notice");
    return notice;
  }

  it("localizes the cap with the CURRENT tier's label and the limit from err.gate", async () => {
    vi.mocked(createContext).mockRejectedValueOnce(
      capRefusal(CURRENT_SERVER_BODY),
    );
    await submitAdvancedCreate();

    // `free` resolves to this deployment's label (S by default); the prose's
    // own "Your S plan" is never parsed.
    const notice = await findRefusalNotice();
    expect(notice).toHaveTextContent(/"currentPlan":"S"/);
    expect(notice).toHaveTextContent(/"limit":1/);
    expect(notice).toHaveTextContent(/"feature":"features\.contexts\./);
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
    // The gate notice is the one rendering; no sentence is shown beside it.
    expect(within(screen.getByRole("dialog")).getAllByRole("alert")).toEqual([
      notice,
    ]);
  });

  it("labels an operator-defined current tier by the matrix's display name (#1645)", async () => {
    // The wire ships no label for the current tier; the shared matrix does.
    mockTiers = [
      ...OSS_TIERS,
      { name: "team_custom", display_name: "Team", max_contexts: 5 },
    ] as unknown as PlanTierFeature[];
    vi.mocked(createContext).mockRejectedValueOnce(
      capRefusal({
        ...CURRENT_SERVER_BODY,
        current: 5,
        limit: 5,
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "team_custom",
      }),
    );
    await submitAdvancedCreate();

    const notice = await findRefusalNotice();
    expect(notice).toHaveTextContent(/"currentPlan":"Team"/);
    expect(notice).toHaveTextContent(/"limit":5/);
  });

  it("does not parse the server prose: a refusal without gate details shows the server text", async () => {
    // A server predating #1644 raised the context cap with no details at
    // all. The old regex pair turned its prose into the localized sentence;
    // nothing reads the prose now.
    vi.mocked(createContext).mockRejectedValueOnce(capRefusal(undefined));
    await submitAdvancedCreate();

    expect(await screen.findByText(SERVER_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/^quota\.title/)).toBeNull();
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

    // #1646: a quota refusal is the gate notice for ITS OWN quota — the API
    // calls limit, with no counts to state — never the context cap.
    const notice = await findRefusalNotice();
    expect(notice).toHaveTextContent(/"feature":"features\.api_calls\./);
    expect(notice).not.toHaveTextContent(/features\.contexts/);
    expect(notice).toHaveTextContent(/quota\.descriptionNoNumbers/);
    expect(within(screen.getByRole("dialog")).getAllByRole("alert")).toEqual([
      notice,
    ]);
  });

  it("does not render another quota that carries a plan and a limit as the context cap", async () => {
    const serverText = "Daily memory limit reached (100/100).";
    const details = {
      ...CURRENT_SERVER_BODY,
      quota_type: "memories_per_day",
      current: 100,
      limit: 100,
    };
    vi.mocked(createContext).mockRejectedValueOnce(
      new ApiError({
        error: "QUOTA-001",
        message: serverText,
        status: 429,
        details,
        gate: normalizeGate(429, "QUOTA-001", details),
      }),
    );
    await submitAdvancedCreate();

    const notice = await findRefusalNotice();
    expect(notice).toHaveTextContent(/"feature":"features\.memories\./);
    expect(notice).not.toHaveTextContent(/features\.contexts/);
    expect(notice).toHaveTextContent(/"limit":100/);
    expect(within(screen.getByRole("dialog")).getAllByRole("alert")).toEqual([
      notice,
    ]);
  });
});

// ---------- #1646 Q2: the create-error notice --------------------------------

describe("ContextsPage create errors: a quota refusal is the gate notice (#1646)", () => {
  const CAP_BODY = {
    gate: "quota",
    quota_type: "contexts",
    current: 1,
    limit: 1,
    required_plan: "basic",
    required_plan_display: "M",
    current_plan: "free",
  };

  function capRefusal(): ApiError {
    return new ApiError({
      error: "QUOTA-001",
      message: "Context limit reached.",
      status: 429,
      details: CAP_BODY,
      gate: normalizeGate(429, "QUOTA-001", CAP_BODY),
    });
  }

  async function submitIn(dialogOpener: "advanced" | "quick") {
    mockUseAuth.mockReturnValue({
      user: { current_workspace_id: WORKSPACE_ID },
      refetchUser: vi.fn(),
    });
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: "free",
        current_user_role: "owner",
      },
    });
    mockGetContexts.mockResolvedValue({ contexts: [] });
    // No embedding → the amber empty state, whose Create opens Quick Create;
    // otherwise the blue one, whose Create opens the advanced dialog.
    const canEmbed = dialogOpener === "advanced";
    mockCheckOpenAIKeyStatus.mockResolvedValue({
      has_key: canEmbed,
      embedding_available: canEmbed,
    });
    mockGetEmbeddingModels.mockResolvedValue({
      models: [],
      default_model: "small",
    });
    render(<ContextsPage />);
    if (!canEmbed) await screen.findByText("setupNeededOpenAI");
    fireEvent.click(await screen.findByRole("button", { name: /^create$/i }));
    fireEvent.change(
      await screen.findByPlaceholderText("contextNamePlaceholder"),
      { target: { value: "my-context" } },
    );
    const dialog = await screen.findByRole("dialog");
    const submit = within(dialog)
      .getAllByRole("button")
      .find((b) => b.textContent === "create");
    if (!submit) throw new Error("no submit button");
    await act(async () => {
      fireEvent.click(submit);
    });
    return dialog;
  }

  it.each(["advanced", "quick"] as const)(
    "%s dialog: the cap refusal is the gate notice, with the CTA for an owner on plan_page",
    async (which) => {
      mockFeatures = { byok: true, plan_page: true };
      vi.mocked(createContext).mockRejectedValueOnce(capRefusal());
      const dialog = await submitIn(which);

      const title = await within(dialog).findByText(/^quota\.title /);
      const notice = title.closest('[role="alert"]') as HTMLElement;
      // The tier that raises the cap, by its label (basic is M here).
      expect(notice).toHaveTextContent(/quota\.upsell \{"plan":"M"/);
      fireEvent.click(
        within(notice).getByRole("button", { name: /^quota\.action/ }),
      );
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
      // The quota refusal is not ALSO rendered as a sentence.
      expect(within(dialog).getAllByRole("alert")).toEqual([notice]);
      expect(within(dialog).queryByText("Context limit reached.")).toBeNull();
    },
  );

  it.each(["advanced", "quick"] as const)(
    "%s dialog: no CTA where the Plan page is off",
    async (which) => {
      vi.mocked(createContext).mockRejectedValueOnce(capRefusal());
      const dialog = await submitIn(which);

      const title = await within(dialog).findByText(/^quota\.title /);
      const notice = title.closest('[role="alert"]') as HTMLElement;
      expect(within(notice).queryByRole("button")).toBeNull();
      expect(notice).not.toHaveTextContent(/quota\.upsell/);
    },
  );

  it.each(["advanced", "quick"] as const)(
    "%s dialog: a non-gate error keeps the string path",
    async (which) => {
      vi.mocked(createContext).mockRejectedValueOnce(
        new ApiError({
          error: "CTX-409",
          message: "Context name already exists",
          status: 409,
        }),
      );
      const dialog = await submitIn(which);

      expect(await within(dialog).findByText("nameTaken")).toBeInTheDocument();
      expect(within(dialog).queryByText(/^quota\./)).toBeNull();
    },
  );

  it("a retry that fails validation shows the validation error, not the earlier refusal", async () => {
    vi.mocked(createContext).mockRejectedValueOnce(capRefusal());
    const dialog = await submitIn("advanced");
    await within(dialog).findByText(/^quota\.title /);

    fireEvent.change(
      within(dialog).getByPlaceholderText("contextNamePlaceholder"),
      {
        target: { value: "" },
      },
    );
    const submit = within(dialog)
      .getAllByRole("button")
      .find((b) => b.textContent === "create");
    await act(async () => {
      fireEvent.click(submit!);
    });

    expect(within(dialog).getByText("nameRequired")).toBeInTheDocument();
    expect(within(dialog).queryByText(/^quota\.title/)).toBeNull();
  });
});
