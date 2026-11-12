/**
 * Account settings → Emails (#2761): load, error, toggle on/off, 409 refetch,
 * hidden while not offered and off, kept while not offered but on.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockGet = vi.fn();
const mockSet = vi.fn();
vi.mock("@/lib/api/announcementEmails", () => ({
  getAnnouncementEmails: () => mockGet(),
  setAnnouncementEmails: (...args: unknown[]) => mockSet(...args),
}));

const { FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number) {
      super("fake");
      this.status = status;
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

import { AnnouncementEmailSettings } from "./AnnouncementEmailSettings";

const OFF = {
  offered: true,
  announcement_emails: false,
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

describe("AnnouncementEmailSettings", () => {
  it("shows a loading state, then the toggle off", async () => {
    mockGet.mockResolvedValue(OFF);
    const onVisibility = vi.fn();
    render(<AnnouncementEmailSettings onVisibilityChange={onVisibility} />);
    const toggle = await screen.findByRole("switch", { name: "toggle" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("description")).toBeInTheDocument();
    expect(onVisibility).toHaveBeenLastCalledWith(true);
  });

  it("shows an error banner when loading fails", async () => {
    mockGet.mockRejectedValue(new FakeApiError(500));
    render(<AnnouncementEmailSettings />);
    expect(await screen.findByText("loadFailed")).toBeInTheDocument();
  });

  it("turns it on with the wording version and shows when it changed", async () => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockResolvedValue({
      ...OFF,
      announcement_emails: true,
      answered: true,
      source: "settings",
      recorded_at: "2026-11-12T09:00:00Z",
      recorded: true,
    });
    render(<AnnouncementEmailSettings />);
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

  it("turns it off", async () => {
    mockGet.mockResolvedValue({
      ...OFF,
      announcement_emails: true,
      answered: true,
    });
    mockSet.mockResolvedValue({ ...OFF, answered: true, recorded: true });
    render(<AnnouncementEmailSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith(false, "2026-11"));
    expect(mockToast).toHaveBeenCalledWith({ title: "savedOff" });
  });

  it("reloads on 409 and says why", async () => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockRejectedValue(new FakeApiError(409));
    render(<AnnouncementEmailSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "stale", variant: "destructive" }),
    );
  });

  it("shows a destructive toast when saving fails", async () => {
    mockGet.mockResolvedValue(OFF);
    mockSet.mockRejectedValue(new FakeApiError(500));
    render(<AnnouncementEmailSettings />);
    fireEvent.click(await screen.findByRole("switch", { name: "toggle" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "failed",
          variant: "destructive",
        }),
      ),
    );
  });

  it("is hidden while not offered and off", async () => {
    mockGet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
    });
    const onVisibility = vi.fn();
    const { container } = render(
      <AnnouncementEmailSettings onVisibilityChange={onVisibility} />,
    );
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(onVisibility).not.toHaveBeenCalledWith(true);
  });

  it("keeps the toggle while not offered but on, so it can be turned off", async () => {
    mockGet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
      announcement_emails: true,
      answered: true,
    });
    mockSet.mockResolvedValue({
      ...OFF,
      offered: false,
      wording_version: null,
      recorded: true,
    });
    render(<AnnouncementEmailSettings />);
    const toggle = await screen.findByRole("switch", { name: "toggle" });
    expect(toggle).not.toBeDisabled();
    expect(screen.getByText("notOffered")).toBeInTheDocument();
    fireEvent.click(toggle);
    await waitFor(() => expect(mockSet).toHaveBeenCalledWith(false, null));
  });
});
