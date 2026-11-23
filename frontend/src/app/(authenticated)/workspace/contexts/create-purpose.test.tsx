/**
 * The advanced create dialog's search purpose (#2983).
 *
 * The default (conversation and work history) is today's create request and
 * nothing else. Documents and manuals sends its weights and fusion through
 * the existing search-config update once the context exists; if that update
 * fails the context stays, and a toast says the purpose was not applied.
 *
 * The Radix select is swapped for a native one (jsdom has no pointer events).
 */

import type { ReactNode } from "react";
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import type { PlanTierFeature } from "@/lib/api/workspaces";
import {
  act,
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";

import ContextsPage from "./page";

const mockGetContexts = vi.fn();
const mockCreateContext = vi.fn();
const mockGetConfig = vi.fn();
const mockUpdateConfig = vi.fn();

vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
  createContext: (...a: unknown[]) => mockCreateContext(...a),
  getContextStats: vi.fn(),
  getContextSearchConfig: (...a: unknown[]) => mockGetConfig(...a),
  updateContextSearchConfig: (...a: unknown[]) => mockUpdateConfig(...a),
  getEmbeddingModels: () =>
    Promise.resolve({ models: [], default_model: "small" }),
}));

vi.mock("@/lib/api/memory-health", () => ({
  UNATTRIBUTED_SCOPE: "unattributed",
  getMemoryHealthBreakdown: () => new Promise(() => {}),
}));

vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: () =>
    Promise.resolve({ has_key: true, embedding_available: true }),
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
  useTranslations: () => (k: string, params?: Record<string, unknown>) =>
    params ? `${k} ${JSON.stringify(params)}` : k,
  useLocale: () => "en",
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    disabled,
    children,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    disabled?: boolean;
    children: ReactNode;
  }) => (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { current_workspace_id: "ws-1" },
    refetchUser: vi.fn(),
  }),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: {
      id: "ws-1",
      plan_name: "pro",
      current_user_role: "owner",
    },
  }),
}));

vi.mock("@/contexts/MemoryContextContext", () => ({
  useMemoryContext: () => ({
    currentContext: null,
    contextId: null,
    contextName: null,
    isLoading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({ byok: true }),
}));

const TIERS = [
  { name: "pro", display_name: "L", max_contexts: 20, shared_contexts: true },
] as unknown as PlanTierFeature[];
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => TIERS,
}));

/** What the create request carries today (the advanced dialog, name only). */
const TODAYS_REQUEST = {
  expected_workspace_id: "ws-1",
  name: "my-docs",
  display_name: undefined,
  description: undefined,
  summary: undefined,
  usage_guide: undefined,
  embedding_model: undefined,
  is_private: true,
};

const STORED = {
  context_id: "new-ctx",
  semantic_weight: 0.6,
  bm25_weight: 0.4,
  fetch_factor: 3,
  use_rerank: false,
  reranker_provider: "voyage",
  reranker_model: "rerank-2",
  fusion: "auto",
  fusion_effective: "rrf",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

async function openDialog(): Promise<HTMLElement> {
  render(<ContextsPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: /^advancedCreate$/i }),
  );
  fireEvent.change(
    await screen.findByPlaceholderText("contextNamePlaceholder"),
    { target: { value: "my-docs" } },
  );
  return screen.findByRole("dialog");
}

function purposeSelect(dialog: HTMLElement): HTMLSelectElement {
  const found = (
    within(dialog).getAllByRole("combobox") as HTMLSelectElement[]
  ).filter((s) => Array.from(s.options).some((o) => o.value === "documents"));
  expect(found).toHaveLength(1);
  return found[0];
}

async function submit(dialog: HTMLElement) {
  const button = within(dialog)
    .getAllByRole("button")
    .find((b) => b.textContent === "create");
  if (!button) throw new Error("no submit button");
  await act(async () => {
    fireEvent.click(button);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetContexts.mockResolvedValue({ contexts: [] });
  mockCreateContext.mockResolvedValue({ id: "new-ctx", name: "my-docs" });
  mockGetConfig.mockResolvedValue(STORED);
  mockUpdateConfig.mockResolvedValue(STORED);
});

afterEach(() => cleanup());

describe("create dialog — search purpose", () => {
  it("offers the two purposes that carry values, conversation by default, with guidance", async () => {
    const dialog = await openDialog();
    const select = purposeSelect(dialog);
    expect(select.value).toBe("conversation");
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "conversation",
      "documents",
    ]);
    expect(within(dialog).getByText("purpose.label")).toBeTruthy();
    expect(within(dialog).getByText("searchPurposeHint")).toBeTruthy();
  });

  it("the default sends today's create request and nothing else", async () => {
    const dialog = await openDialog();
    await submit(dialog);
    await waitFor(() => expect(mockCreateContext).toHaveBeenCalledTimes(1));
    expect(mockCreateContext.mock.calls[0][0]).toEqual(TODAYS_REQUEST);
    expect(Object.keys(mockCreateContext.mock.calls[0][0]).sort()).toEqual(
      Object.keys(TODAYS_REQUEST).sort(),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mockGetConfig).not.toHaveBeenCalled();
    expect(mockUpdateConfig).not.toHaveBeenCalled();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("documents sends its values through the search-config update after the create", async () => {
    const dialog = await openDialog();
    fireEvent.change(purposeSelect(dialog), { target: { value: "documents" } });
    await submit(dialog);
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    // The create request itself is unchanged.
    expect(mockCreateContext.mock.calls[0][0]).toEqual(TODAYS_REQUEST);
    expect(mockUpdateConfig.mock.calls[0][0]).toBe("new-ctx");
    expect(mockUpdateConfig.mock.calls[0][1]).toEqual({
      semantic_weight: 0.8,
      bm25_weight: 0.2,
      fetch_factor: 3,
      use_rerank: false,
      reranker_provider: "voyage",
      reranker_model: "rerank-2",
      fusion: "auto",
    });
    expect(mockCreateContext.mock.invocationCallOrder[0]).toBeLessThan(
      mockUpdateConfig.mock.invocationCallOrder[0],
    );
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("documents on a server that predates the fusion setting sends the weights only", async () => {
    const older: Record<string, unknown> = { ...STORED };
    delete older.fusion;
    delete older.fusion_effective;
    mockGetConfig.mockResolvedValue(older);
    const dialog = await openDialog();
    fireEvent.change(purposeSelect(dialog), { target: { value: "documents" } });
    await submit(dialog);
    await waitFor(() => expect(mockUpdateConfig).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig.mock.calls[0][1]).not.toHaveProperty("fusion");
    expect(mockUpdateConfig.mock.calls[0][1]).toMatchObject({
      semantic_weight: 0.8,
      bm25_weight: 0.2,
    });
  });

  it("a failed update keeps the context and says the purpose was not applied", async () => {
    mockUpdateConfig.mockRejectedValue(new Error("boom"));
    const dialog = await openDialog();
    fireEvent.change(purposeSelect(dialog), { target: { value: "documents" } });
    await submit(dialog);
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    expect(mockToast.mock.calls[0][0]).toMatchObject({
      description: "searchPurposeNotApplied",
      variant: "destructive",
    });
    // No rollback: the dialog closed as on any successful create, the list
    // was refreshed, and nothing was deleted.
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mockCreateContext).toHaveBeenCalledTimes(1);
    expect(mockGetContexts.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("a failed create sends no update", async () => {
    mockCreateContext.mockRejectedValue(new Error("nope"));
    const dialog = await openDialog();
    fireEvent.change(purposeSelect(dialog), { target: { value: "documents" } });
    await submit(dialog);
    await waitFor(() => expect(mockCreateContext).toHaveBeenCalledTimes(1));
    expect(mockUpdateConfig).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("the purpose resets to the default for the next context", async () => {
    let dialog = await openDialog();
    fireEvent.change(purposeSelect(dialog), { target: { value: "documents" } });
    await submit(dialog);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(
      await screen.findByRole("button", { name: /^advancedCreate$/i }),
    );
    dialog = await screen.findByRole("dialog");
    expect(purposeSelect(dialog).value).toBe("conversation");
  });
});
