/**
 * Tests for the first-run onboarding card (Issue #952).
 *
 * Covers the trigger gating (the part with real logic) and the in-app
 * create→save→recall happy path that delivers the "value moment".
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  cleanup,
} from "@testing-library/react";

let mockUserId = "u1";
const mockRefetchUser = vi.fn();
const mockUpdateProfile = vi.fn();
const mockSetLocale = vi.fn();
const mockCopy = vi.fn();
let mockProfile = { timezone: "UTC", locale: "en" };
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: mockUserId, ...mockProfile }, refetchUser: mockRefetchUser }),
}));
vi.mock("@/i18n", () => ({
  defaultLocale: "en",
  locales: ["en", "ja"],
  localeNames: { en: "English", ja: "日本語" },
  useLocale: () => ({ locale: "en", setLocale: mockSetLocale }),
}));
vi.mock("@/lib/api/base", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/base")>()),
  updateUserProfile: (...a: unknown[]) => mockUpdateProfile(...a),
}));
let mockCopied = false;
vi.mock("@/hooks/useCopyFeedback", () => ({
  useCopyFeedback: () => ({ isCopied: () => mockCopied, copyToTarget: (...a: unknown[]) => mockCopy(...a) }),
}));
const mockPush = vi.fn();
const mockToast = vi.fn();
const mockGetContexts = vi.fn();
const mockCreateContext = vi.fn();
const mockRemember = vi.fn();
const mockRecall = vi.fn();
const mockCheckKey = vi.fn();
const mockUseWorkspace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

// Identity translator: returns the key so assertions can match on keys.
vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
  createContext: (...a: unknown[]) => mockCreateContext(...a),
}));

vi.mock("@/lib/api/memory", () => ({
  rememberMemory: (...a: unknown[]) => mockRemember(...a),
  recallMemories: (...a: unknown[]) => mockRecall(...a),
}));

vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: (...a: unknown[]) => mockCheckKey(...a),
}));

// #1167: the key probe is gated on features.byok; default on, flip per-test.
let mockFeatures: Record<string, boolean> | null = { byok: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

import { OnboardingCard } from "./OnboardingCard";
import { ApiError } from "@/lib/api/base";

function setWorkspace(role: string = "owner") {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", current_user_role: role },
    currentWorkspaceId: "ws-1",
    loading: false,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request: async (_key: string, run: () => boolean) => run() } });
  window.localStorage.clear();
  // The practice tests start past the first screens (#2701 has its own block).
  window.localStorage.setItem("onboarding:u1:intro", "done");
  mockProfile = { timezone: "UTC", locale: "en" };
  mockRefetchUser.mockResolvedValue({ id: "u1" });
  mockUpdateProfile.mockResolvedValue({});
  mockCopy.mockResolvedValue(undefined);
  mockCopied = false;
  mockUserId = "u1";
  setWorkspace("owner");
  mockFeatures = { byok: true };
  mockGetContexts.mockResolvedValue({ contexts: [], total: 0 });
  mockCheckKey.mockResolvedValue({
    has_key: true,
    can_configure: true,
    external_keys_url: "/workspace/settings/keys",
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "languages");
});

describe("OnboardingCard trigger gating", () => {
  it("keeps a failed context lookup retryable from the practice launcher", async () => {
    mockGetContexts.mockRejectedValueOnce(new Error("offline"));
    render(<OnboardingCard replay />);
    expect(await screen.findByText("errors.loadFailed")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "retryLoad" }));
    expect(await screen.findByText("context.createButton")).toBeVisible();
    expect(mockGetContexts).toHaveBeenCalledTimes(2);
  });

  it("closes practice when a retry discovers existing non-practice contexts", async () => {
    mockGetContexts.mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ contexts: [{ id: "existing" }] });
    const onClose = vi.fn();
    render(<OnboardingCard replay onClose={onClose} />);
    fireEvent.click(await screen.findByRole("button", { name: "retryLoad" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onClose).toHaveBeenCalledOnce();
    expect(mockCreateContext).not.toHaveBeenCalled();
  });

  it("keeps the dashboard card hidden when the context lookup fails", async () => {
    mockGetContexts.mockRejectedValueOnce(new Error("offline"));
    render(<OnboardingCard />);
    await waitFor(() => expect(mockGetContexts).toHaveBeenCalledOnce());
    // Let the rejected probe settle; the dialog renders in a portal, so look at the screen.
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("errors.loadFailed")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("onboarding:u1:ws-1:wizard")).toBeNull();
  });

  it("closes a load error without recording a dismissal", async () => {
    mockGetContexts.mockRejectedValueOnce(new Error("offline"));
    const onClose = vi.fn();
    render(<OnboardingCard replay onClose={onClose} />);
    await screen.findByText("errors.loadFailed");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onClose).toHaveBeenCalledOnce();
    expect(window.localStorage.getItem("onboarding:u1:ws-1:wizard")).toBeNull();
  });

  it("does not probe again when the parent re-renders with a new onClose", async () => {
    const { rerender } = render(<OnboardingCard replay onClose={() => {}} />);
    await screen.findByText("context.createButton");
    rerender(<OnboardingCard replay onClose={() => {}} />);
    await screen.findByText("context.createButton");
    expect(mockGetContexts).toHaveBeenCalledOnce();
  });

  it.each(["create", "save"])("ends a definite %s conflict without uncertain-write recovery", async operation => {
    const conflict = new ApiError({ status: 409, error: "RES-002", message: "English server prose", details: { reason: "workspace_changed" } });
    if (operation === "save") {
      window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "memory", contextId: "c1" }));
      mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
      mockRemember.mockRejectedValueOnce(conflict);
    } else {
      mockCreateContext.mockRejectedValueOnce(conflict);
    }
    render(<OnboardingCard replay />);
    fireEvent.click(await screen.findByText(operation === "create" ? "context.createButton" : "memory.saveButton"));
    expect(await screen.findByText("errors.workspaceChanged")).toBeVisible();
    expect(screen.queryByText("recovery")).not.toBeInTheDocument();
    expect(screen.queryByText("English server prose")).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("onboarding:u1:ws-1:wizard")!).step).toBe("ended");
  });

  it("keeps completed practice terminal after its context is deleted", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "recall", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
    mockRecall.mockResolvedValueOnce({ results: [] });
    const first = render(<OnboardingCard replay />);
    fireEvent.click(await screen.findByText("recall.searchButton"));
    await screen.findByText("done.title");
    first.unmount();
    mockGetContexts.mockClear().mockResolvedValue({ contexts: [] });
    const second = render(<OnboardingCard replay />);
    await waitFor(() => expect(second.container).toBeEmptyDOMElement());
    expect(mockGetContexts).not.toHaveBeenCalled();
    expect(mockCreateContext).not.toHaveBeenCalled();
  });
  it("shows the create-context step for an owner with zero contexts + a key", async () => {
    render(<OnboardingCard />);
    await waitFor(() =>
      expect(screen.getByText("context.createButton")).toBeInTheDocument(),
    );
  });

  it("renders nothing when the user already has contexts", async () => {
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "c1" }],
      total: 1,
    });
    const { container } = render(<OnboardingCard />);
    await waitFor(() => expect(mockGetContexts).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a member (cannot create contexts)", async () => {
    setWorkspace("member");
    const { container } = render(<OnboardingCard />);
    // role gate short-circuits before any contexts fetch
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(mockGetContexts).not.toHaveBeenCalled();
  });

  it("renders nothing when previously dismissed", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "context", dismissed: true }));
    const { container } = render(<OnboardingCard />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(mockGetContexts).not.toHaveBeenCalled();
  });

  it("lets the platform-supplied embedding credential power the sample", async () => {
    mockCheckKey.mockResolvedValue({ has_key: false, embedding_available: true });
    render(<OnboardingCard />);
    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
  });

  it("does not block an older server that knows only whether the workspace has a key", async () => {
    mockCheckKey.mockResolvedValue({ has_key: false });
    render(<OnboardingCard />); expect(await screen.findByText("context.createButton")).toBeInTheDocument();
  });
  it("withholds the save step when a create response belongs to another workspace", async () => {
    mockCreateContext.mockResolvedValue({ id: "other", workspace_id: "ws-B", created_by: "u1", is_private: true });
    render(<OnboardingCard />); fireEvent.click(await screen.findByText("context.createButton"));
    expect(await screen.findByText("recovery")).toBeInTheDocument();
    expect(mockRemember).not.toHaveBeenCalled();
  });

  it("resumes Recall only for a still-visible practice context without saving a sample twice", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "recall", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
    render(<OnboardingCard />);
    expect(await screen.findByText("recall.searchButton")).toBeInTheDocument();
    expect(mockCreateContext).not.toHaveBeenCalled(); expect(mockRemember).not.toHaveBeenCalled();
  });

  it("shows a recovery path for a lost write response without repeating the write", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "saving", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
    render(<OnboardingCard />);
    expect(await screen.findByText("recovery")).toBeInTheDocument();
    expect(screen.queryByText("memory.saveButton")).toBeNull();
    expect(mockRemember).not.toHaveBeenCalled();
  });

  it("ends an uncertain practice and keeps manual replay read-only", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "creating" }));
    const first = render(<OnboardingCard replay />);
    fireEvent.click(await screen.findByText("endPractice"));
    first.unmount();
    const again = render(<OnboardingCard replay />);
    await waitFor(() => expect(again.container).toBeEmptyDOMElement());
    expect(JSON.parse(localStorage.getItem("onboarding:u1:ws-1:wizard")!).step).toBe("ended");
    expect(mockCreateContext).not.toHaveBeenCalled(); expect(mockRemember).not.toHaveBeenCalled();
  });

  it("does not resume a sample after access is removed or owner is demoted", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "memory", contextId: "gone" }));
    const page = render(<OnboardingCard replay />);
    expect(await screen.findByText("recovery")).toBeInTheDocument();
    expect(screen.queryByText("memory.saveButton")).toBeNull();
    setWorkspace("admin"); page.rerender(<OnboardingCard replay />);
    expect(page.container).toBeEmptyDOMElement(); expect(mockRemember).not.toHaveBeenCalled();
  });

  it("two resumed forms can claim the same sample write only once", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "memory", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
    mockRemember.mockResolvedValue({ status: "success" });
    render(<><OnboardingCard /><OnboardingCard /></>);
    const [first, second] = await screen.findAllByText("memory.saveButton");
    fireEvent.click(first); fireEvent.click(second);
    expect(await screen.findByText("recall.searchButton")).toBeInTheDocument();
    expect(await screen.findByText("recovery")).toBeInTheDocument();
    expect(mockRemember).toHaveBeenCalledTimes(1);
    expect(mockRemember).toHaveBeenCalledWith(expect.objectContaining({ private_workspace_id: "ws-1" }));
  });

  it("skips the key probe and shows the normal flow when byok is off (#1167)", async () => {
    mockFeatures = { byok: false };
    render(<OnboardingCard />);
    await waitFor(() =>
      expect(screen.getByText("context.createButton")).toBeInTheDocument(),
    );
    // Probe never fired — the API 404s in a BYOK-off deployment and env
    // keys serve embeddings, so no needsKey notice either.
    expect(mockCheckKey).not.toHaveBeenCalled();
    expect(screen.queryByText("needsKey.title")).toBeNull();
  });

  it("shows the embedding-key notice (not the flow) when no key is configured, and its CTA uses the real /workspace route (not the backend value)", async () => {
    mockCheckKey.mockResolvedValue({
      has_key: false,
      embedding_available: false,
      can_configure: true,
      // The backend returns this prefix-less (404-ing) value; the card must NOT use it.
      external_keys_url: "/integrations/external-keys",
    });
    render(<OnboardingCard />);
    await waitFor(() =>
      expect(screen.getByText("needsKey.title")).toBeInTheDocument(),
    );
    expect(screen.queryByText("context.createButton")).not.toBeInTheDocument();
    // MCP writes need the same embedding key, so the key comes first.
    expect(screen.queryByRole("link", { name: "mcp.link" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("needsKey.button"));
    expect(mockPush).toHaveBeenCalledWith(
      "/workspace/integrations/external-keys",
    );
  });
});

describe("OnboardingCard value-moment happy path", () => {
  it("closes a recovery replay before opening the contexts list", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "creating" }));
    const onClose = vi.fn();
    render(<OnboardingCard replay onClose={onClose} />);
    fireEvent.click(await screen.findByText("openContexts"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onClose).toHaveBeenCalledOnce();
    expect(mockPush).toHaveBeenCalledWith("/workspace/contexts");
    expect(mockCreateContext).not.toHaveBeenCalled();
    expect(mockRemember).not.toHaveBeenCalled();
  });

  it("walks create → save → recall and shows the recalled hit", async () => {
    mockCreateContext.mockResolvedValue({ id: "ctx-1", name: "x", workspace_id: "ws-1", is_private: true, created_by: "u1" });
    mockRemember.mockResolvedValue({
      status: "success",
      memory_id: "m1",
      scope: "persistent",
    });
    mockRecall.mockResolvedValue({
      results: [
        {
          memory_id: "m1",
          summary: "the saved sample memory",
          context_summary: null,
          type: "note",
          importance: 0.7,
          scope: "persistent",
          created_at: "2026-06-13T00:00:00Z",
          client: "web",
          tags: [],
          context: null,
          score: 0.92,
        },
      ],
      related_tags: [],
    });

    render(<OnboardingCard />);

    // Step 1: create the default context without entering a name
    await waitFor(() =>
      expect(screen.getByText("context.createButton")).toBeInTheDocument(),
    );
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.queryByLabelText("context.nameLabel")).not.toBeInTheDocument();
    expect(screen.getByText("context.lead")).toBeInTheDocument();
    fireEvent.click(screen.getByText("context.createButton"));

    // Step 2: save memory
    await waitFor(() =>
      expect(screen.getByText("memory.saveButton")).toBeInTheDocument(),
    );
    expect(mockCreateContext).toHaveBeenCalledWith({
      name: expect.stringMatching(/^my-first-context-[0-9a-f-]{36}$/),
      display_name: "My First Context",
      is_private: true,
      expected_workspace_id: "ws-1",
    });
    fireEvent.click(screen.getByText("memory.saveButton"));

    // Step 3: recall
    await waitFor(() =>
      expect(screen.getByText("recall.searchButton")).toBeInTheDocument(),
    );
    expect(mockRemember).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "note",
        context: { context_id: "ctx-1" },
      }),
    );
    fireEvent.click(screen.getByText("recall.searchButton"));

    // Done: the recalled hit is shown (the value moment)
    await waitFor(() =>
      expect(screen.getByText("the saved sample memory")).toBeInTheDocument(),
    );
    expect(mockRecall).toHaveBeenCalledWith(
      expect.objectContaining({ filters: { context_id: "ctx-1" } }),
    );
    expect(screen.getByText("done.title")).toBeInTheDocument();

    // The MCP pointer opens the connection card on the Integrations entry,
    // not the API keys tab: clients register over OAuth (#2698).
    const links = screen.getAllByRole("link", { name: "mcp.link" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/workspace/integrations#mcp-connection");
  });

  it("shows a fallback (not a blank screen) when recall returns no results", async () => {
    mockCreateContext.mockResolvedValue({ id: "ctx-1", name: "x", workspace_id: "ws-1", is_private: true, created_by: "u1" });
    mockRemember.mockResolvedValue({
      status: "success",
      memory_id: "m1",
      scope: "persistent",
    });
    mockRecall.mockResolvedValue({ results: [], related_tags: [] });

    render(<OnboardingCard />);
    await waitFor(() =>
      expect(screen.getByText("context.createButton")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("context.createButton"));
    await waitFor(() =>
      expect(screen.getByText("memory.saveButton")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("memory.saveButton"));
    await waitFor(() =>
      expect(screen.getByText("recall.searchButton")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("recall.searchButton"));

    await waitFor(() =>
      expect(screen.getByText("recall.noResults")).toBeInTheDocument(),
    );
    expect(screen.getByText("done.title")).toBeInTheDocument();
  });

  it("shows a failed creation inside the dialog", async () => {
    mockCreateContext.mockRejectedValueOnce(new Error("offline"));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByText("context.createButton"));
    expect(await screen.findByText("errors.createFailed")).toBeVisible();
    expect(screen.getByRole("dialog")).toContainElement(screen.getByText("errors.createFailed"));
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("lets a pending Recall be dismissed", async () => {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "recall", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
    mockRecall.mockReturnValue(new Promise(() => {}));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByText("recall.searchButton"));
    expect(mockRecall).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "dismiss" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps the guide open while a create is pending", async () => {
    mockCreateContext.mockReturnValue(new Promise(() => {}));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByText("context.createButton"));
    await waitFor(() => expect(screen.getByRole("button", { name: "dismiss" })).toBeDisabled());
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("queues a dismissal behind another tab's claim so neither is lost", async () => {
    let tail: Promise<unknown> = Promise.resolve();
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request: (_name: string, run: () => unknown) => { const result = tail.then(run); tail = result.catch(() => undefined); return result; } },
    });
    render(<OnboardingCard />);
    await screen.findByText("context.createButton");
    // Another tab is inside its claim: it read "context" and is about to write "creating".
    let claim: (() => void) | undefined;
    void navigator.locks.request("onboarding:u1:ws-1", () => new Promise<void>(resolve => {
      claim = () => { localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "creating" })); resolve(); };
    }));
    fireEvent.click(screen.getByRole("button", { name: "dismiss" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(claim).toBeDefined());
    claim!();
    await waitFor(() => expect(JSON.parse(localStorage.getItem("onboarding:u1:ws-1:wizard")!)).toEqual({ step: "creating", dismissed: true }));
  });

  it("runs the practice without Web Locks or crypto.randomUUID (plain-HTTP self-hosting)", async () => {
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    const randomUUID = Object.getOwnPropertyDescriptor(crypto, "randomUUID");
    Object.defineProperty(crypto, "randomUUID", { configurable: true, value: undefined });
    try {
      mockCreateContext.mockResolvedValue({ id: "ctx-1", workspace_id: "ws-1", is_private: true, created_by: "u1" });
      render(<OnboardingCard />);
      fireEvent.click(await screen.findByText("context.createButton"));
      expect(await screen.findByText("memory.saveButton")).toBeInTheDocument();
      expect(mockCreateContext).toHaveBeenCalledWith(expect.objectContaining({
        name: expect.stringMatching(/^my-first-context-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/),
      }));
      expect(JSON.parse(localStorage.getItem("onboarding:u1:ws-1:wizard")!)).toEqual({ step: "memory", contextId: "ctx-1" });
    } finally {
      if (randomUUID) Object.defineProperty(crypto, "randomUUID", randomUUID);
      else Reflect.deleteProperty(crypto, "randomUUID");
    }
  });

  it("dismiss persists to localStorage and hides the card", async () => {
    render(<OnboardingCard />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "dismiss" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "dismiss" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(JSON.parse(window.localStorage.getItem("onboarding:u1:ws-1:wizard")!).dismissed).toBe(true);
  });
});

describe("OnboardingCard first screens (#2701)", () => {
  beforeEach(() => {
    window.localStorage.removeItem("onboarding:u1:intro");
  });

  function browserIn(languages: string[], timeZone: string) {
    Object.defineProperty(navigator, "languages", { configurable: true, value: languages });
    vi.spyOn(Intl, "DateTimeFormat").mockImplementation(
      (() => ({ resolvedOptions: () => ({ timeZone }) })) as unknown as typeof Intl.DateTimeFormat,
    );
  }

  /** The first context exists: the practice resumes at "save a memory". */
  function withPracticeContext() {
    window.localStorage.setItem("onboarding:u1:ws-1:wizard", JSON.stringify({ step: "memory", contextId: "c1" }));
    mockGetContexts.mockResolvedValue({ contexts: [{ id: "c1", workspace_id: "ws-1", is_private: true, created_by: "u1" }] });
  }

  it("asks for the language and timezone first, prefilled from the browser, and saves them before switching the language", async () => {
    browserIn(["ja-JP", "en-US"], "Asia/Tokyo");
    render(<OnboardingCard />);
    expect(await screen.findByText("prefs.title")).toBeInTheDocument();
    expect(screen.getByText("prefs.detected")).toBeInTheDocument();
    expect(screen.queryByText("context.createButton")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "prefs.save" }));

    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
    expect(mockUpdateProfile).toHaveBeenCalledWith({ locale: "ja", timezone: "Asia/Tokyo" });
    // The layout re-applies the saved user locale, so the refresh comes first.
    expect(mockRefetchUser.mock.invocationCallOrder[0]).toBeLessThan(mockSetLocale.mock.invocationCallOrder[0]);
    expect(mockSetLocale).toHaveBeenCalledWith("ja");
    expect(localStorage.getItem("onboarding:u1:intro")).toBe("chat");
  });

  it("uses the browser timezone even off the common list, and keeps a language the browser does not offer", async () => {
    browserIn(["fr-FR"], "Europe/Berlin");
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.save" }));
    await screen.findByText("context.createButton");
    expect(mockUpdateProfile).toHaveBeenCalledWith({ locale: "en", timezone: "Europe/Berlin" });
  });

  it("keeps values the user already chose instead of the browser's", async () => {
    mockProfile = { timezone: "Asia/Kolkata", locale: "ja" };
    browserIn(["en-US"], "America/New_York");
    render(<OnboardingCard />);
    expect(await screen.findByText("prefs.title")).toBeInTheDocument();
    expect(screen.queryByText("prefs.detected")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "prefs.save" }));
    await screen.findByText("context.createButton");
    expect(mockUpdateProfile).toHaveBeenCalledWith({ locale: "ja", timezone: "Asia/Kolkata" });
  });

  it("asks once per user, not again for another workspace", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "done");
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { id: "ws-2", current_user_role: "owner" },
      currentWorkspaceId: "ws-2",
      loading: false,
    });
    render(<OnboardingCard />);
    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
    expect(screen.queryByText("prefs.title")).not.toBeInTheDocument();
  });

  it("stays on the screen with the error inside the dialog when the save fails", async () => {
    mockUpdateProfile.mockRejectedValueOnce(new Error("offline"));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.save" }));
    expect(await screen.findByText("prefs.saveFailed")).toBeVisible();
    expect(screen.getByRole("dialog")).toContainElement(screen.getByText("prefs.saveFailed"));
    expect(screen.getByText("prefs.title")).toBeInTheDocument();
    expect(mockSetLocale).not.toHaveBeenCalled();
    expect(localStorage.getItem("onboarding:u1:intro")).toBeNull();
  });

  it("treats a failed refresh after the save as a failed save, so the language is not switched back", async () => {
    mockRefetchUser.mockResolvedValueOnce(undefined);
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.save" }));
    expect(await screen.findByText("prefs.saveFailed")).toBeVisible();
    expect(mockSetLocale).not.toHaveBeenCalled();
    expect(localStorage.getItem("onboarding:u1:intro")).toBeNull();
  });

  it("cannot be closed while the save is in flight", async () => {
    mockUpdateProfile.mockReturnValueOnce(new Promise(() => {}));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.save" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "dismiss" })).toBeDisabled());
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("skip saves nothing and moves on to creating the first context", async () => {
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.skip" }));
    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
    expect(mockUpdateProfile).not.toHaveBeenCalled();
    expect(mockSetLocale).not.toHaveBeenCalled();
  });

  it("shows the chat screen right after the first context is created, then the optional practice", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    mockCreateContext.mockResolvedValue({ id: "ctx-1", workspace_id: "ws-1", is_private: true, created_by: "u1" });
    render(<OnboardingCard />);
    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
    expect(screen.queryByText("chat.title")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("context.createButton"));

    expect(await screen.findByText("chat.title")).toBeInTheDocument();
    expect(screen.queryByText("memory.saveButton")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "chat.next" }));
    expect(await screen.findByText("memory.saveButton")).toBeInTheDocument();
    expect(screen.getByText("memory.optional")).toBeInTheDocument();
    expect(localStorage.getItem("onboarding:u1:intro")).toBe("done");
  });

  it("shows the MCP URL to paste into a chat client and the per-client steps", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    render(<OnboardingCard />);
    expect(await screen.findByText("chat.title")).toBeInTheDocument();
    const url = screen.getByText(/\/mcp$/);
    fireEvent.click(screen.getByRole("button", { name: "chat.copy" }));
    expect(mockCopy).toHaveBeenCalledWith(url.textContent, expect.any(String));
    expect(screen.getByRole("link", { name: "chat.guide" })).toHaveAttribute(
      "href",
      "/workspace/integrations#mcp-connection",
    );
  });

  it("announces a successful copy to screen readers", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    mockCopied = true;
    render(<OnboardingCard />);
    const status = await screen.findByText("chat.copied");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("keeps a failed copy inside the dialog with the copy-by-hand hint", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    mockCopy.mockRejectedValueOnce(new Error("denied"));
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "chat.copy" }));
    expect(await screen.findByText("copyFailedManualHint")).toBeVisible();
    expect(screen.getByRole("dialog")).toContainElement(screen.getByText("copyFailedManualHint"));
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("counts the chat screen as seen when the user leaves for the per-client steps", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("link", { name: "chat.guide" }));
    expect(localStorage.getItem("onboarding:u1:intro")).toBe("done");
  });

  it("shows the embedding-key notice before the chat screen", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    mockCheckKey.mockResolvedValue({ has_key: false, embedding_available: false });
    render(<OnboardingCard />);
    expect(await screen.findByText("needsKey.title")).toBeInTheDocument();
    expect(screen.queryByText("chat.title")).not.toBeInTheDocument();
  });

  it("ignores a browser timezone the platform does not know", async () => {
    browserIn(["en-US"], "Etc/Unknown");
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("button", { name: "prefs.save" }));
    await screen.findByText("context.createButton");
    expect(mockUpdateProfile).toHaveBeenCalledWith({ locale: "en", timezone: "UTC" });
  });

  it("keeps the chat screen when the per-client steps open in a new tab", async () => {
    window.localStorage.setItem("onboarding:u1:intro", "chat");
    withPracticeContext();
    render(<OnboardingCard />);
    fireEvent.click(await screen.findByRole("link", { name: "chat.guide" }), { ctrlKey: true });
    expect(localStorage.getItem("onboarding:u1:intro")).toBe("chat");
    expect(screen.getByText("chat.title")).toBeInTheDocument();
  });

  it("a replay from Getting started goes straight to the practice", async () => {
    render(<OnboardingCard replay />);
    expect(await screen.findByText("context.createButton")).toBeInTheDocument();
    expect(screen.queryByText("prefs.title")).not.toBeInTheDocument();
  });
});
