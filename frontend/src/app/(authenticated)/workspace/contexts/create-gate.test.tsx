/**
 * The "New Context" control must reflect the SERVER's rules (#1487).
 *
 * A Pro workspace with 3 contexts and no BYOK key got a dead button and no
 * explanation, because the page disabled it on `hasOpenAIKey === false` — a
 * precondition the backend does not have — and the explanatory alert only
 * rendered in the `contexts.length === 0` branch.
 *
 * `quota-gate.test.ts` pins the arithmetic. This file renders the actual page,
 * because the arithmetic being right did not stop the button being dead: the
 * regression to prevent is a page-level one.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import type { PlanTierFeature } from "@/lib/api/workspaces";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";

import ContextsPage from "./page";

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/workspace/contexts",
}));

// Echoes interpolation vars, not just the key. The quota banner's whole bug
// was that it stated a plan and a limit that were not the workspace's own, and
// a mock that drops vars cannot see the difference (#1488 Phase 4).
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns?: string) => (k: string, vars?: Record<string, unknown>) =>
      vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
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

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

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

const WORKSPACE_ID = "ws-1";

function ctx(n: number) {
  // `sleep_mode` is REQUIRED: SleepModeBadge does
  // `const { Icon, ... } = MODE_CONFIG[mode]`, so an omitted or unknown mode
  // throws and unmounts the whole page. The pre-existing page test only ever
  // used an empty list, so no fixture had exercised a rendered row before.
  return Array.from({ length: n }, (_, i) => ({
    id: `c${i}`,
    name: `ctx-${i}`,
    memory_count: 0,
    sleep_mode: "full" as const,
  }));
}

function setup(opts: {
  plan?: string;
  maxContexts?: number;
  contextCount?: number;
  visible?: number;
  hasKey?: boolean;
  /** #1495: embedding availability, which a platform credential can supply
   *  even when the workspace owns no key. Defaults to `hasKey`. */
  canEmbed?: boolean;
  role?: string;
}) {
  const visible = opts.visible ?? opts.contextCount ?? 0;
  mockUseAuth.mockReturnValue({
    user: { current_workspace_id: WORKSPACE_ID },
    refetchUser: vi.fn(),
  });
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: {
      id: WORKSPACE_ID,
      plan_name: opts.plan ?? "pro",
      max_contexts: opts.maxContexts,
      context_count: opts.contextCount ?? visible,
      current_user_role: opts.role ?? "owner",
    },
  });
  mockGetContexts.mockResolvedValue({ contexts: ctx(visible) });
  // #1495: the gate now asks whether embedding WORKS, not whether this
  // workspace owns a key. `canEmbed` defaults to `hasKey` so every existing
  // case keeps its meaning — in those scenarios there is no platform
  // credential, so the two coincide.
  mockCheckOpenAIKeyStatus.mockResolvedValue({
    has_key: opts.hasKey ?? true,
    embedding_available: opts.canEmbed ?? opts.hasKey ?? true,
  });
  mockGetEmbeddingModels.mockResolvedValue({
    models: [],
    default_model: "small",
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMemoryContext.mockReturnValue({
    currentContext: null,
    contextId: null,
    contextName: null,
    isLoading: false,
    error: null,
    refresh: vi.fn(),
  });
  mockFeatures = { byok: true };
  mockTiers = OSS_TIERS;
});

afterEach(() => cleanup());

/** The header create control, which is the one the report was about. */
async function newContextButton() {
  return await screen.findByRole("button", { name: /newContext/i });
}

describe("New Context control", () => {
  it("is ENABLED for a Pro workspace with contexts and no key (the reported bug)", async () => {
    setup({ plan: "pro", maxContexts: 20, contextCount: 3, hasKey: false });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
  });

  it("still explains the missing key when the workspace already has contexts", async () => {
    // The guidance used to live only in the empty state, so this exact user saw
    // nothing at all.
    setup({ plan: "pro", maxContexts: 20, contextCount: 3, hasKey: false });
    render(<ContextsPage />);
    expect(await screen.findByText("setupNeededOpenAI")).toBeInTheDocument();
  });

  it("does not demand a key when the platform already supplies one", async () => {
    // #1495. The workspace owns no key and does not need one — the deployment
    // sets OPENAI_API_KEY, so embedding works and creation must not be gated.
    //
    // This is the shape #1487 shipped once already: the client re-deriving a
    // server rule it cannot see, and telling a healthy workspace it is broken.
    // Here it was live in production — every workspace served by the platform
    // credential saw a red "OpenAI API key required" banner and a warning
    // triangle while embedding 100% successfully.
    setup({
      plan: "pro",
      maxContexts: 20,
      contextCount: 3,
      hasKey: false,
      canEmbed: true,
    });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
    expect(screen.queryByText("setupNeededOpenAI")).not.toBeInTheDocument();
  });

  it("stays CLICKABLE at the cap, so the quota explanation is reachable", async () => {
    // The trigger must not be disabled: the only route to the quota dialog is
    // a menu item inside this dropdown, so disabling the trigger is what made
    // that dialog dead code. Creation is still blocked — by the item handlers,
    // which open the dialog instead of the create form.
    setup({ plan: "pro", maxContexts: 20, contextCount: 20, hasKey: true });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
  });

  it("warns on screen when the cap is reached", async () => {
    // Whatever the control does, the reason has to be visible — a silent block
    // is the whole of #1487.
    setup({ plan: "pro", maxContexts: 20, contextCount: 20, hasKey: true });
    render(<ContextsPage />);
    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
  });

  it("does not warn about the cap when there is room", async () => {
    setup({ plan: "pro", maxContexts: 20, contextCount: 3, hasKey: true });
    render(<ContextsPage />);
    await waitFor(async () => expect(await newContextButton()).toBeTruthy());
    expect(screen.queryByText(/quotaReachedDetail/)).not.toBeInTheDocument();
  });

  it("is enabled for a free workspace whose cap was raised by config", async () => {
    // PLAN_FREE_MAX_CONTEXTS=5. The old rule blocked at 1 regardless.
    setup({ plan: "free", maxContexts: 5, contextCount: 1, hasKey: true });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
  });

  it("counts the workspace stat, not just the contexts it can SEE", async () => {
    // GET /contexts hides other users' private contexts, so an admin can see 1
    // of 20. Trusting the visible list would enable a button the server
    // rejects.
    setup({
      plan: "pro",
      maxContexts: 20,
      contextCount: 20,
      visible: 1,
      hasKey: true,
    });
    render(<ContextsPage />);
    // Seeing 1 of 20 must still register as "at the cap" — otherwise the UI
    // promises a create the server rejects.
    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
  });

  it("a cap of 0 (a tier that excludes contexts) still blocks, as before (#1645)", async () => {
    // quotaGate reads limit 0 as "unknown, never block"; the page keeps the
    // zero cap's own block so wrapping the rule in the descriptor changes
    // nothing a user sees.
    setup({ plan: "free", maxContexts: 0, contextCount: 0, hasKey: true });
    render(<ContextsPage />);
    expect(await screen.findByText(/quotaReachedDetail/)).toBeInTheDocument();
  });

  it("does not block when the server did not send a cap", async () => {
    setup({ plan: "pro", maxContexts: undefined, contextCount: 99 });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
  });

  it("names the workspace's OWN plan and cap, not the free-plan rule", async () => {
    // The defect this replaces: the gate was widened in #1487 to "any plan at
    // the server-sent cap", but the banner kept asserting the rule it no longer
    // used — a Pro workspace at 20/20 was told "Free plan allows 1 context.
    // Upgrade to Basic or Pro". Every clause of that was false, and telling a
    // paying user a wrong reason is the same failure #1487 was filed for.
    setup({ plan: "pro", maxContexts: 20, contextCount: 20, hasKey: true });
    render(<ContextsPage />);

    const banner = await screen.findByText(/quotaReachedDetail/);
    expect(banner.textContent).toContain('"plan":"pro"');
    expect(banner.textContent).toContain('"limit":20');
  });

  it("states a basic workspace's own cap too", async () => {
    setup({ plan: "basic", maxContexts: 3, contextCount: 3, hasKey: true });
    render(<ContextsPage />);

    const banner = await screen.findByText(/quotaReachedDetail/);
    expect(banner.textContent).toContain('"plan":"basic"');
    expect(banner.textContent).toContain('"limit":3');
  });
});

// #1645: one gate — the tier matrix's `shared_contexts` — decides the shared
// option in both create dialogs, instead of 12 literal / ordinal reads of the
// plan name. #1646: ContextPrivacyChoice renders it with the gate.* copy, so
// the badge is `plan.badge`, the line under the option `plan.description` and
// the CTA `plan.action` (this file's mock echoes the key and its arguments).
const BADGE = /^plan\.badge/;
const REFUSAL = /^plan\.description:/;
const CTA = /^plan\.action/;

describe("Shared option in the create dialog (#1645)", () => {
  async function openAdvancedCreate() {
    // An empty workspace offers the Advanced create dialog directly.
    const create = await screen.findByRole("button", { name: "create" });
    fireEvent.click(create);
    await screen.findByText(/sharedOption/);
    return document.querySelector(
      'input[type="radio"][value="shared"]',
    ) as HTMLInputElement;
  }

  it("the shared radio and its helper text agree while the plan is unresolved", async () => {
    // Before: the literal compare left the radio ENABLED while the ordinal
    // pro-or-better check, false for an unknown plan, already printed the
    // upsell.
    mockTiers = null;
    setup({ plan: "pro", maxContexts: 20, contextCount: 0 });
    render(<ContextsPage />);

    const radio = await openAdvancedCreate();
    expect(radio).toBeDisabled();
    expect(screen.queryByText(REFUSAL)).toBeNull();
    expect(screen.queryByText("teamMembersAccess")).toBeNull();
    expect(screen.queryByText(BADGE)).toBeNull();
  });

  it("a tier without shared contexts: inert radio, badge and upsell copy", async () => {
    setup({ plan: "basic", maxContexts: 3, contextCount: 0 });
    render(<ContextsPage />);

    const radio = await openAdvancedCreate();
    expect(radio).toBeDisabled();
    expect(screen.getByText(REFUSAL)).toBeInTheDocument();
    expect(screen.getByText(BADGE)).toBeInTheDocument();
    // The tier that lifts it, by its resolved label; the radio is described
    // by the refusal.
    expect(screen.getByText(BADGE).textContent).toContain('"plan":"L"');
    expect(radio).toHaveAccessibleDescription(REFUSAL);
  });

  it("a tier with shared contexts: the radio works", async () => {
    setup({ plan: "pro", maxContexts: 20, contextCount: 0 });
    render(<ContextsPage />);

    const radio = await openAdvancedCreate();
    expect(radio).not.toBeDisabled();
    expect(screen.getByText("teamMembersAccess")).toBeInTheDocument();
    fireEvent.click(radio);
    expect(radio).toBeChecked();
  });

  it("no served tier has shared contexts: the explanation stays, the CTA does not (#1645)", async () => {
    // Even for an owner on a plan_page deployment: there is no tier to buy.
    mockFeatures = { byok: true, plan_page: true };
    mockTiers = OSS_TIERS.map((t) => ({ ...t, shared_contexts: false }));
    setup({ plan: "basic", maxContexts: 3, contextCount: 0, role: "owner" });
    render(<ContextsPage />);

    const radio = await openAdvancedCreate();
    expect(radio).toBeDisabled();
    // No tier to name: the no-tier sentence, and no badge.
    expect(screen.getByText(/^plan\.descriptionNoTier/)).toBeInTheDocument();
    expect(screen.queryByText(BADGE)).toBeNull();
    expect(screen.queryByRole("button", { name: CTA })).toBeNull();
  });

  it("follows the matrix, not the tier name: an operator gives basic shared contexts", async () => {
    mockTiers = OSS_TIERS.map((t) =>
      t.name === "basic" ? { ...t, shared_contexts: true } : t,
    );
    setup({ plan: "basic", maxContexts: 3, contextCount: 0 });
    render(<ContextsPage />);

    const radio = await openAdvancedCreate();
    expect(radio).not.toBeDisabled();
    expect(screen.queryByText(BADGE)).toBeNull();
  });
});

// #1646: both create dialogs render ONE ContextPrivacyChoice — the shared
// option's refusal is the same gate copy in each; only the private option's
// helper line is the dialog's own.
describe("Privacy choice in both create dialogs (#1646)", () => {
  function sharedRadio() {
    return document.querySelector(
      'input[type="radio"][value="shared"]',
    ) as HTMLInputElement;
  }

  it("the advanced dialog: the gate's refusal and CTA, and its own private helper", async () => {
    mockFeatures = { byok: true, plan_page: true };
    setup({ plan: "basic", maxContexts: 3, contextCount: 0 });
    render(<ContextsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "create" }));
    expect(await screen.findByText("createDialogTitle")).toBeInTheDocument();

    expect(sharedRadio()).toBeDisabled();
    expect(screen.getByText(REFUSAL)).toBeInTheDocument();
    expect(screen.getByText(BADGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: CTA })).toBeInTheDocument();
    expect(screen.getByText("privateAvailableAllPlans")).toBeInTheDocument();
    expect(screen.queryByText("onlyYouCanAccess")).toBeNull();
  });

  it("the quick dialog: the same refusal and CTA, and its own private helper", async () => {
    mockFeatures = { byok: true, plan_page: true };
    // No embedding: the amber empty state's Create opens Quick Create.
    setup({ plan: "basic", maxContexts: 3, contextCount: 0, hasKey: false });
    render(<ContextsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "create" }));
    expect(await screen.findByText("quickCreateContext")).toBeInTheDocument();

    expect(sharedRadio()).toBeDisabled();
    expect(screen.getByText(REFUSAL)).toBeInTheDocument();
    expect(screen.getByText(BADGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: CTA })).toBeInTheDocument();
    expect(screen.getByText("onlyYouCanAccess")).toBeInTheDocument();
    expect(screen.queryByText("privateAvailableAllPlans")).toBeNull();
  });

  it("the quick dialog: an admin's private option is inert with the quick helper", async () => {
    setup({
      plan: "pro",
      maxContexts: 20,
      contextCount: 0,
      hasKey: false,
      role: "admin",
    });
    render(<ContextsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "create" }));
    expect(await screen.findByText("quickCreateContext")).toBeInTheDocument();

    const priv = document.querySelector(
      'input[type="radio"][value="private"]',
    ) as HTMLInputElement;
    expect(priv).toBeDisabled();
    expect(screen.getByText("adminsCanOnlyCreateShared")).toBeInTheDocument();
    // Admins create shared contexts: the dialog pre-selects Shared.
    expect(sharedRadio()).toBeChecked();
  });
});

// #1645: the shared option over every cell of {tier matrix} x {/system/info}
// x {role, tier}. A failed matrix reads as `null`, like a pending one (the
// hook suite pins that). The option's CTA is the gate's own `canUpgrade`:
// owner only, and only where the Plan page is known to be on.
describe("Shared option in the create dialog — the whole truth table (#1645)", () => {
  const MATRIX: Record<string, PlanTierFeature[] | null> = {
    "pending-or-failed": null,
    resolved: OSS_TIERS,
  };
  const INFO: Record<string, Record<string, boolean> | null> = {
    pending: null,
    "plan_page on": { byok: true, plan_page: true },
    "plan_page off": { byok: true, plan_page: false },
    "failed ({})": {},
  };
  const CELLS = Object.keys(MATRIX).flatMap((m) =>
    Object.keys(INFO).flatMap((i) =>
      (["admin", "owner"] as const).flatMap((role) =>
        (["basic", "pro"] as const).map((plan) => [m, i, role, plan] as const),
      ),
    ),
  );

  it.each(CELLS)(
    "matrix %s, /system/info %s, %s on %s",
    async (m, i, role, plan) => {
      mockTiers = MATRIX[m];
      mockFeatures = INFO[i];
      setup({ plan, maxContexts: 20, contextCount: 0, role });
      render(<ContextsPage />);
      fireEvent.click(await screen.findByRole("button", { name: "create" }));
      await screen.findByText(/sharedOption/);
      const radio = document.querySelector(
        'input[type="radio"][value="shared"]',
      ) as HTMLInputElement;

      const known = m === "resolved";
      const entitled = known && plan === "pro";
      const refused = known && plan === "basic";

      expect(radio.disabled).toBe(!entitled);
      // Helper text and badge agree with the radio; pending says nothing.
      expect(screen.queryByText("teamMembersAccess") !== null).toBe(entitled);
      expect(screen.queryByText(REFUSAL) !== null).toBe(refused);
      expect(screen.queryByText(BADGE) !== null).toBe(refused);
      // The CTA: a refusal, the owner, and a Plan page known to be on.
      expect(screen.queryByRole("button", { name: CTA }) !== null).toBe(
        refused && role === "owner" && i === "plan_page on",
      );
    },
  );
});
