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
import { render, screen, waitFor, cleanup } from "@testing-library/react";

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
      vars && Object.keys(vars).length > 0
        ? `${k}:${JSON.stringify(vars)}`
        : k,
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
    setup({ plan: "pro", maxContexts: 20, contextCount: 3, hasKey: false, canEmbed: true });
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
