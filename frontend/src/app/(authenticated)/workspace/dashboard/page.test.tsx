/**
 * Dashboard permalink to a context (#2936): `?context=<id>` selects the
 * context, the selection keeps the address in step, a copy control puts the
 * absolute link on the clipboard, and a link that does not name one of this
 * workspace's contexts leaves the dashboard intact with a short notice.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import WorkspaceStatsPage from "./page";
import { ClipboardCopyError } from "@/lib/utils/clipboard";

const CTX_A = "11111111-1111-4111-8111-111111111111";
const CTX_B = "22222222-2222-4222-8222-222222222222";
const CTX_OTHER = "33333333-3333-4333-8333-333333333333";

// ---------- Mocks ------------------------------------------------------------

const mockPush = vi.fn();
const mockReplace = vi.fn();
let mockQuery = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  useSearchParams: () => new URLSearchParams(mockQuery),
  usePathname: () => "/workspace/dashboard",
}));

// Keys echo back; an interpolated name is appended so a test can see which
// context a message was given.
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns?: string) => (key: string, values?: Record<string, unknown>) =>
      values && "name" in values ? `${key}:${String(values.name)}` : key,
  useLocale: () => "en",
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1", linked_user_ids: [] } }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockCopyText = vi.fn();
vi.mock("@/lib/utils/clipboard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/utils/clipboard")>();
  return { ...actual, copyText: (...args: unknown[]) => mockCopyText(...args) };
});

const mockApiGet = vi.fn();
vi.mock("@/lib/api/base", () => ({
  apiClient: { get: (...args: unknown[]) => mockApiGet(...args) },
}));

const mockGetContextStats = vi.fn();
const mockGetTimeline = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getContextStats: (...args: unknown[]) => mockGetContextStats(...args),
  getWorkspaceMemoryTimeline: (...args: unknown[]) => mockGetTimeline(...args),
}));

// Heavy neighbours that this feature does not touch.
vi.mock("@/components/dashboard/UsageStats", () => ({
  UsageStats: () => null,
}));
vi.mock("@/components/dashboard/UsageTrends", () => ({
  UsageTrends: () => null,
}));
vi.mock("@/components/dashboard/KpiCards", () => ({ KpiCards: () => null }));
vi.mock("@/components/dashboard/MemoryTimelineChart", () => ({
  MemoryTimelineChart: () => null,
}));
vi.mock("@/components/dashboard/AdminSections", () => ({
  AdminSections: () => null,
}));
vi.mock("@/components/dashboard/QuotaOverview", () => ({
  QuotaNotice: () => null,
}));
vi.mock("@/components/onboarding/OnboardingCard", () => ({
  OnboardingCard: () => null,
}));
vi.mock("@/components/common/PlanBadge", () => ({ PlanBadge: () => null }));

// ---------- Fixtures ---------------------------------------------------------

function workspace(role: string) {
  return {
    currentWorkspace: {
      id: "ws-1",
      name: "Team",
      description: "",
      current_user_role: role,
    },
    currentWorkspaceId: "ws-1",
    loading: false,
  };
}

const statsFixture = {
  plan_name: "pro",
  total_memories: 30,
  context_count: 2,
  contexts: [
    {
      context_id: CTX_A,
      context_name: "Alpha",
      created_by: "user-1",
      created_by_name: "Me",
      memory_count: 10,
      is_private: false,
    },
    {
      context_id: CTX_B,
      context_name: "Beta",
      created_by: "user-1",
      created_by_name: "Me",
      memory_count: 20,
      is_private: false,
    },
  ],
};

const contextStatsFixture = {
  contexts: [],
  total_contexts: 2,
  workspace_totals: { memory_count: 30, active_users_week: 0 },
};

function renderPage() {
  return render(<WorkspaceStatsPage />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockQuery = "";
  window.location.hash = "";
  mockUseWorkspace.mockReturnValue(workspace("owner"));
  mockApiGet.mockResolvedValue(statsFixture);
  mockGetContextStats.mockResolvedValue(contextStatsFixture);
  mockGetTimeline.mockResolvedValue(null);
  mockCopyText.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("dashboard context permalink (#2936)", () => {
  it("selects the context named by ?context= when it is in the workspace", async () => {
    mockQuery = `context=${CTX_B}`;
    renderPage();

    // The selector shows Beta and the page asked for Beta's timeline.
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, CTX_B),
    );
    expect(
      screen.getByRole("button", { name: "copyContextLink:Beta" }),
    ).toBeInTheDocument();
    // No notice for a context that is in the workspace.
    expect(screen.queryByText("contextLinkUnavailable")).toBeNull();
    // The parameter already names the selection: nothing to rewrite.
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("replaces the address when the selection changes and drops the parameter for all contexts", async () => {
    mockQuery = `context=${CTX_A}&tab=x`;
    renderPage();
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, CTX_A),
    );

    // Radix Select: open with the keyboard, choose "all".
    const trigger = screen.getByRole("combobox", {
      name: "filterByContext",
    });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "allContexts" }));

    expect(mockReplace).toHaveBeenCalledTimes(1);
    // Other parameters stay; history is not extended (replace, not push).
    expect(mockReplace).toHaveBeenCalledWith(
      "/workspace/dashboard?tab=x",
      expect.objectContaining({ scroll: false }),
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("writes the chosen context into the address", async () => {
    renderPage();
    const trigger = await screen.findByRole("combobox", {
      name: "filterByContext",
    });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Beta" }));

    expect(mockReplace).toHaveBeenCalledWith(
      `/workspace/dashboard?context=${CTX_B}`,
      expect.objectContaining({ scroll: false }),
    );
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, CTX_B),
    );
  });

  it("offers no copy control next to the selector while all contexts are shown", async () => {
    renderPage();
    await screen.findByRole("combobox", { name: "filterByContext" });
    expect(
      screen.queryByRole("button", { name: "copyContextLink:Alpha" }),
    ).toBeNull();
  });

  it("copies the exact absolute permalink and confirms it", async () => {
    mockQuery = `context=${CTX_A}`;
    renderPage();
    const button = await screen.findByRole("button", {
      name: "copyContextLink:Alpha",
    });

    await act(async () => {
      fireEvent.click(button);
    });

    expect(mockCopyText).toHaveBeenCalledWith(
      `${window.location.origin}/workspace/dashboard?context=${CTX_A}`,
    );
    expect(mockToast).toHaveBeenCalledWith({
      description: "contextLinkCopied",
    });
  });

  it("shows the existing clipboard error message when the copy fails", async () => {
    mockQuery = `context=${CTX_A}`;
    mockCopyText.mockRejectedValue(new ClipboardCopyError());
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    renderPage();
    const button = await screen.findByRole("button", {
      name: "copyContextLink:Alpha",
    });

    await act(async () => {
      fireEvent.click(button);
    });

    expect(mockToast).toHaveBeenCalledWith({
      title: "error",
      description: "copyFailed",
      variant: "destructive",
    });
    expect(mockToast).not.toHaveBeenCalledWith({
      description: "contextLinkCopied",
    });
    consoleError.mockRestore();
  });

  it("copies the link of the row whose button is used in the context list", async () => {
    window.location.hash = "#dashboard-contexts";
    renderPage();
    const row = (await screen.findByRole("link", { name: "Beta" })).closest(
      "tr",
    ) as HTMLElement;

    await act(async () => {
      fireEvent.click(
        within(row).getByRole("button", { name: "copyContextLink:Beta" }),
      );
    });

    expect(mockCopyText).toHaveBeenCalledWith(
      `${window.location.origin}/workspace/dashboard?context=${CTX_B}`,
    );
    // Every row has its own, named after its context.
    expect(
      screen.getByRole("button", { name: "copyContextLink:Alpha" }),
    ).toBeInTheDocument();
  });

  it("shows all contexts and a notice with the detail link for a UUID that is not in the workspace", async () => {
    mockQuery = `context=${CTX_OTHER}`;
    renderPage();

    expect(await screen.findByText("contextLinkUnavailable")).toBeVisible();
    const link = screen.getByRole("link", { name: "openLinkedContext" });
    expect(link).toHaveAttribute("href", `/workspace/contexts/${CTX_OTHER}`);
    // All contexts: the selector stays on "all" and the timeline is unfiltered.
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith(
        "ws-1",
        30,
        undefined,
      ),
    );
    expect(
      screen.queryByRole("button", { name: /^copyContextLink:/ }),
    ).toBeNull();
    // The notice does not say whether the context exists.
    expect(screen.getByText("contextLinkUnavailable").textContent).toBe(
      "contextLinkUnavailable",
    );
  });

  it("lets the notice be dismissed", async () => {
    mockQuery = `context=${CTX_OTHER}`;
    renderPage();
    await screen.findByText("contextLinkUnavailable");

    fireEvent.click(screen.getByRole("button", { name: "close" }));

    expect(screen.queryByText("contextLinkUnavailable")).toBeNull();
  });

  it("ignores a value that is not a UUID, without a notice", async () => {
    // A listed context's id with its hyphens removed is not a UUID.
    mockQuery = `context=${CTX_A.replaceAll("-", "")}`;
    renderPage();
    const selector = await screen.findByRole("combobox", {
      name: "filterByContext",
    });

    expect(selector).toHaveTextContent("allContexts");
    expect(screen.queryByText("contextLinkUnavailable")).toBeNull();
    expect(screen.queryByRole("link", { name: "openLinkedContext" })).toBeNull();
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, undefined),
    );
    expect(mockGetTimeline).not.toHaveBeenCalledWith(
      "ws-1",
      30,
      expect.anything(),
    );
  });

  it("goes back to all contexts when the selected context is gone after a refresh", async () => {
    mockQuery = `context=${CTX_B}`;
    renderPage();
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, CTX_B),
    );

    mockApiGet.mockResolvedValue({
      ...statsFixture,
      contexts: statsFixture.contexts.filter((c) => c.context_id !== CTX_B),
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /refresh/ }));
    });

    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, undefined),
    );
    expect(mockReplace).toHaveBeenCalledWith("/workspace/dashboard", {
      scroll: false,
    });
    expect(screen.queryByText("contextLinkUnavailable")).toBeNull();
    expect(
      screen.getByRole("combobox", { name: "filterByContext" }),
    ).toHaveTextContent("allContexts");
  });

  it("sends a viewer with a linked context to that context's page", async () => {
    mockUseWorkspace.mockReturnValue(workspace("viewer"));
    mockQuery = `context=${CTX_A}`;
    renderPage();

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith(`/workspace/contexts/${CTX_A}`),
    );
    expect(mockApiGet).not.toHaveBeenCalled();
  });

  it("sends a viewer without a link, or with a non-UUID value, to the context list", async () => {
    mockUseWorkspace.mockReturnValue(workspace("viewer"));
    mockQuery = "context=../admin";
    renderPage();

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/contexts"),
    );
    expect(mockPush).not.toHaveBeenCalledWith(
      expect.stringContaining("admin"),
    );

    cleanup();
    mockPush.mockClear();
    mockQuery = "";
    renderPage();
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/contexts"),
    );
  });

  it("gives the copy controls an accessible name with the context's name and a tooltip", async () => {
    mockQuery = `context=${CTX_A}`;
    renderPage();
    const button = await screen.findByRole("button", {
      name: "copyContextLink:Alpha",
    });
    expect(button).toHaveAttribute("title", "copyContextLink:Alpha");
    expect(button.tagName).toBe("BUTTON");
  });

  it("clears the selection when ?context= is removed from outside the page", async () => {
    mockQuery = `context=${CTX_A}`;
    const { rerender } = renderPage();
    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, CTX_A),
    );

    mockQuery = "";
    rerender(<WorkspaceStatsPage />);

    await waitFor(() =>
      expect(mockGetTimeline).toHaveBeenLastCalledWith("ws-1", 30, undefined),
    );
    expect(
      screen.queryByRole("button", { name: "copyContextLink:Alpha" }),
    ).toBeNull();
  });

  it("keeps the selector row's width when a context is picked (reserved slot)", async () => {
    renderPage();
    await screen.findByRole("combobox", { name: "filterByContext" });
    const slot = screen.getByTestId("context-link-slot");
    expect(slot).toHaveClass("h-9", "w-9");
    expect(slot.querySelector("button")).toBeNull();

    cleanup();
    mockQuery = `context=${CTX_A}`;
    renderPage();
    const button = await screen.findByRole("button", {
      name: "copyContextLink:Alpha",
    });
    expect(screen.getByTestId("context-link-slot")).toContainElement(button);
  });
});
