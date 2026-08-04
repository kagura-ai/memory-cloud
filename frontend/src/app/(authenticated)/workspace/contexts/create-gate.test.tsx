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
  mockCheckOpenAIKeyStatus.mockResolvedValue({ has_key: opts.hasKey ?? true });
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

  it("is DISABLED only when the server-sent cap is actually reached", async () => {
    setup({ plan: "pro", maxContexts: 20, contextCount: 20, hasKey: true });
    render(<ContextsPage />);
    await waitFor(async () => expect(await newContextButton()).toBeDisabled());
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
    await waitFor(async () => expect(await newContextButton()).toBeDisabled());
  });

  it("does not block when the server did not send a cap", async () => {
    setup({ plan: "pro", maxContexts: undefined, contextCount: 99 });
    render(<ContextsPage />);
    await waitFor(async () =>
      expect(await newContextButton()).not.toBeDisabled(),
    );
  });
});
