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
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user: { id: mockUserId } }) }));
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

afterEach(() => cleanup());

describe("OnboardingCard trigger gating", () => {
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

    // The MCP pointer routes to the real credentials page, not the
    // page-less /workspace/integrations segment (which 404s).
    fireEvent.click(screen.getByText("mcp.link"));
    expect(mockPush).toHaveBeenCalledWith(
      "/workspace/integrations/credentials?tab=api-keys",
    );
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
