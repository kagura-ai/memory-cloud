/**
 * Account settings → Notices in AI tools (#2762): load, error, toggle on/off,
 * 409 (stale wording or no longer offered) refetch, hidden while not offered
 * and off, kept while not offered but on.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockGet = vi.fn();
const mockSet = vi.fn();
vi.mock("@/lib/api/mcpNotices", () => ({
  getMcpNotices: () => mockGet(),
  setMcpNotices: (...args: unknown[]) => mockSet(...args),
}));

const { FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    readonly error?: string;
    constructor(status: number, error?: string) {
      super("fake");
      this.status = status;
      this.error = error;
    }
  }
  return { FakeApiError };
});
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "UTC" } }),
}));
const stableT = (k: string, v?: Record<string, unknown>) =>
  v ? `${k}:${JSON.stringify(v)}` : k;
vi.mock("next-intl", () => ({ useTranslations: () => stableT }));

import { McpNoticeSettings } from "./McpNoticeSettings";

const OFF = {
  offered: true,
  mcp_notices: false,
  answered: false,
  source: null,
  recorded_at: null,
  wording_version: "2026-11",
};

beforeEach(() => {
  mockGet.mockReset();
  mockSet.mockReset();
  mockToast.mockReset();
});

describe("McpNoticeSettings", () => {
  it("shows a loading state, then the toggle off", async () => {
    let resolve: (value: typeof OFF) => void = () => {};
    mockGet.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const onVisibility = vi.fn();
    render(<McpNoticeSettings onVisibilityChange={onVisibility} />);
    expect(screen.queryByRole("switch")).toBeNull();
    expect(onVisibility).not.toHaveBeenCalledWith(true);

    resolve(OFF);
    const toggle = await screen.findByRole("switch", { name: "toggle" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("description")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-notice-settings")).toBeInTheDocument();
    expect(onVisibility).toHaveBeenLastCalledWith(true);
  });

  it("shows an error banner when loading fails", async () => {
    mockGet.mockRejectedValue(new FakeApiError(500));
    const onVisibility = vi.fn();
    render(<McpNoticeSettings onVisibilityChange={onVisibility} />);
    expect(await screen.findByText("loadFailed")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
    await waitFor(() => expect(onVisibility).toHaveBeenLastCalledWith(true));
  });

  it("turns it on with the wording version and shows when it changed", async () => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockResolvedValue({
      ...OFF,
      mcp_notices: true,
      answered: true,
      source: "settings",
      recorded_at: "2026-11-13T09:00:00Z",
      recorded: true,
    });
    render(<McpNoticeSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith(true, "2026-11"));
    await waitFor(() =>
      expect(screen.getByRole("switch")).toHaveAttribute(
        "aria-checked",
        "true",
      ),
    );
    expect(screen.getByText(/^lastChanged:/)).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith({ title: "saved" });
  });

  it("disables the toggle while saving", async () => {
    mockGet.mockResolvedValue(OFF);
    let resolve: (value: unknown) => void = () => {};
    mockSet.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    render(<McpNoticeSettings />);
    const toggle = await screen.findByRole("switch", { name: "toggle" });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toBeDisabled());
    fireEvent.click(toggle);
    expect(mockSet).toHaveBeenCalledTimes(1);

    resolve({ ...OFF, mcp_notices: true, answered: true, recorded: true });
    await waitFor(() => expect(toggle).not.toBeDisabled());
  });

  it("turns it off", async () => {
    mockGet.mockResolvedValue({ ...OFF, mcp_notices: true, answered: true });
    mockSet.mockResolvedValue({ ...OFF, answered: true, recorded: true });
    render(<McpNoticeSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith(false, "2026-11"));
    expect(mockToast).toHaveBeenCalledWith({ title: "savedOff" });
  });

  it.each([
    ["MCPNOTICE-006", "stale"],
    // The card disappears after the reload, so the toast does not ask the
    // person to review it and try again.
    ["MCPNOTICE-005", "notOfferedNow"],
  ])("reloads on 409 %s and says why", async (code, message) => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockRejectedValue(new FakeApiError(409, code));
    render(<McpNoticeSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: message,
        variant: "destructive",
      }),
    );
  });

  it("hides the card when the reload after a 409 says it is no longer offered", async () => {
    mockGet.mockResolvedValueOnce(OFF).mockResolvedValueOnce({
      ...OFF,
      offered: false,
      wording_version: null,
    });
    mockSet.mockRejectedValue(new FakeApiError(409, "MCPNOTICE-005"));
    const onVisibility = vi.fn();
    const { container } = render(
      <McpNoticeSettings onVisibilityChange={onVisibility} />,
    );
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(onVisibility).toHaveBeenLastCalledWith(false);
  });

  it("shows a destructive toast when saving fails", async () => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockRejectedValue(new FakeApiError(429));
    render(<McpNoticeSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "failed",
          variant: "destructive",
        }),
      ),
    );
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("is hidden while not offered and off", async () => {
    mockGet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
    });
    const onVisibility = vi.fn();
    const { container } = render(
      <McpNoticeSettings onVisibilityChange={onVisibility} />,
    );
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(onVisibility).not.toHaveBeenCalledWith(true);
  });

  it("keeps the toggle while not offered but on, so it can be turned off", async () => {
    mockGet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
      mcp_notices: true,
      answered: true,
    });
    mockSet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
      recorded: true,
    });
    render(<McpNoticeSettings />);
    const toggle = await screen.findByRole("switch", { name: "toggle" });
    expect(toggle).not.toBeDisabled();
    expect(screen.getByText("notOffered")).toBeInTheDocument();
    fireEvent.click(toggle);
    await waitFor(() => expect(mockSet).toHaveBeenCalledWith(false, null));
  });
});
