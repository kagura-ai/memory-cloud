/**
 * Tests for the Admin Sleep Reports page "Run Now" button (Issue #247).
 *
 * Covers the three button states described in the issue:
 *   - idle: renders "Run Now" when the page is ready
 *   - running: shows the pending label and disables itself while the POST
 *     is in flight
 *   - disabled: stays disabled during the initial GET reports load
 *
 * Also sanity-checks the toast messages for success and for the 409
 * structured conflict response.
 */

import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
} from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import AdminSleepReportsPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

// Stable references so useCallback/useEffect deps do not invalidate on
// every render and spin up an infinite loadReports loop.
const stableTranslator = (key: string, values?: Record<string, unknown>) => {
  if (values && Object.keys(values).length > 0) {
    return `${key}:${JSON.stringify(values)}`;
  }
  return key;
};
const stableToastCtx = { toast: mockToast };
const stableAuthCtx = { user: { timezone: "UTC", role: "admin" } };

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => stableToastCtx,
}));

vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => stableAuthCtx,
}));

// next/link renders a plain anchor in tests.
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

// Keep layout components minimal so queries stay focused on the buttons.
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ actions }: { actions?: React.ReactNode }) => (
    <div data-testid="page-header">{actions}</div>
  ),
}));
vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/Section", () => ({
  Section: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/LoadingState", () => ({
  LoadingState: () => <div data-testid="loading-state" />,
  InlineSpinner: () => <span data-testid="inline-spinner" />,
}));

// ---------- Helpers ----------------------------------------------------------

const EMPTY_REPORTS = { reports: [], total: 0, limit: 50, offset: 0 };

/**
 * Render the page and wait for the initial loadReports() to settle so the
 * button is in its idle state.
 */
async function renderReady() {
  mockGet.mockResolvedValueOnce(EMPTY_REPORTS);
  const utils = render(<AdminSleepReportsPage />);
  await waitFor(() => {
    expect(
      screen.getByRole("button", { name: "actions.runNow" }),
    ).not.toBeDisabled();
  });
  return utils;
}

// ---------- Tests ------------------------------------------------------------

describe("AdminSleepReportsPage — Run Now button", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockToast.mockReset();
  });

  it("renders in the idle state once the initial load finishes", async () => {
    await renderReady();

    const button = screen.getByRole("button", { name: "actions.runNow" });
    expect(button).toBeInTheDocument();
    expect(button).not.toBeDisabled();
    // Idle label, not the pending one.
    expect(button).toHaveTextContent("actions.runNow");
    expect(button).not.toHaveTextContent("actions.runNowPending");
  });

  it("is disabled while the initial reports request is in flight", () => {
    // Never-resolving GET: page stays in loading=true.
    mockGet.mockReturnValueOnce(new Promise(() => {}));
    render(<AdminSleepReportsPage />);

    const button = screen.getByRole("button", { name: "actions.runNow" });
    expect(button).toBeDisabled();
  });

  it("enters the running state while the POST is in flight and shows the success toast", async () => {
    await renderReady();

    // Next two responses are consumed by handleRunNow: the POST, and the
    // reload GET that fires on success.
    let resolvePost: (value: { report_ids: string[] }) => void = () => {};
    mockPost.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePost = resolve;
      }),
    );
    mockGet.mockResolvedValueOnce(EMPTY_REPORTS);

    const button = screen.getByRole("button", { name: "actions.runNow" });
    fireEvent.click(button);

    // Button shows the pending label and is disabled mid-request.
    await waitFor(() => {
      expect(button).toBeDisabled();
    });
    expect(button).toHaveTextContent("actions.runNowPending");

    // Resolve the POST to completion.
    await act(async () => {
      resolvePost({ report_ids: ["11111111-1111-1111-1111-111111111111"] });
    });

    await waitFor(() => {
      expect(button).not.toBeDisabled();
    });
    expect(button).toHaveTextContent("actions.runNow");

    expect(mockPost).toHaveBeenCalledWith("/api/v1/admin/sleep/run", {
      context_id: null,
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "messages.runStarted" }),
    );
  });

  it("shows the 409 conflict toast with a link to the running report", async () => {
    await renderReady();

    const runningReportId = "22222222-2222-2222-2222-222222222222";
    mockPost.mockRejectedValueOnce({
      status: 409,
      error: "sleep_run_in_progress",
      message: "A sleep run is already in progress for this user.",
      details: {
        running_report_id: runningReportId,
        started_at: "2026-04-09T11:30:00Z",
      },
    });

    const button = screen.getByRole("button", { name: "actions.runNow" });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "messages.runConflict",
          variant: "destructive",
        }),
      );
    });

    // The toast description must be a ReactNode linking to the running
    // report's detail page so the admin can jump straight to it.
    const toastCall = mockToast.mock.calls.at(-1)?.[0];
    const { render: renderNode } = await import("@testing-library/react");
    const { container: descContainer } = renderNode(
      <>{toastCall.description}</>,
    );
    const link = descContainer.querySelector("a");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe(
      `/admin/sleep-reports/${runningReportId}`,
    );

    // Button recovers to idle.
    expect(button).not.toBeDisabled();
    expect(button).toHaveTextContent("actions.runNow");
    // No redundant reload on conflict.
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("hides the Run Now button for non-admin users", async () => {
    // Swap the auth mutation for this single render.
    stableAuthCtx.user.role = "member";
    try {
      mockGet.mockResolvedValueOnce(EMPTY_REPORTS);
      render(<AdminSleepReportsPage />);

      await waitFor(() => {
        expect(
          screen.queryByRole("button", { name: "actions.runNow" }),
        ).toBeNull();
      });
      // Refresh remains available — only the trigger is admin-gated.
      expect(
        screen.getByRole("button", { name: /actions\.refresh/ }),
      ).toBeInTheDocument();
    } finally {
      stableAuthCtx.user.role = "admin";
    }
  });
});
