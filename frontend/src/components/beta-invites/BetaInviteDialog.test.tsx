/**
 * BetaInviteDialog (#1582): the invite URL is a credential shown exactly once,
 * the create button explains itself at the cap, and revoke is confirmed.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockCopyText = vi.hoisted(() => vi.fn());
vi.mock("@/lib/utils/clipboard", () => ({
  copyText: (...a: unknown[]) => mockCopyText(...a),
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) =>
      vars && Object.keys(vars).length > 0
        ? `${key}:${JSON.stringify(vars)}`
        : key,
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

import { ApiError } from "@/lib/api/base";
import type { BetaInvite, BetaInviteSummary } from "@/lib/api/beta-invites";
import { BetaInviteDialog } from "./BetaInviteDialog";

const INVITE_URL = "https://app.example.com/join/tok_SECRET_once";

const invite = (over: Partial<BetaInvite> = {}): BetaInvite => ({
  id: "inv-1",
  status: "active",
  created_at: "2030-01-01T00:00:00Z",
  expires_at: "2030-01-08T00:00:00Z",
  redeemed_at: null,
  revoked_at: null,
  ...over,
});

const summary = (over: Partial<BetaInviteSummary> = {}): BetaInviteSummary => ({
  quota: 4,
  used: 1,
  remaining: 3,
  invites: [invite()],
  ...over,
});

const mockCreate = vi.fn();
const mockRevoke = vi.fn();

function renderDialog(
  props: Partial<React.ComponentProps<typeof BetaInviteDialog>> = {},
) {
  const all = {
    open: true,
    onOpenChange: vi.fn(),
    summary: summary(),
    error: null,
    create: mockCreate,
    revoke: mockRevoke,
    ...props,
  };
  return { ...render(<BetaInviteDialog {...all} />), props: all };
}

const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map(
  (level) => vi.spyOn(console, level),
);

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  mockCreate.mockResolvedValue({
    id: "inv-2",
    url: INVITE_URL,
    expires_at: "2030-01-09T00:00:00Z",
  });
  mockRevoke.mockResolvedValue(undefined);
  mockCopyText.mockResolvedValue(undefined);
});

afterEach(() => {
  // The URL is a credential: nothing in this dialog may log or persist it.
  for (const spy of consoleSpies) {
    expect(JSON.stringify(spy.mock.calls)).not.toContain("tok_SECRET_once");
  }
  const stored = JSON.stringify([
    { ...window.localStorage },
    { ...window.sessionStorage },
  ]);
  expect(stored).not.toContain("tok_SECRET_once");
});

describe("BetaInviteDialog header", () => {
  it("shows used / quota", () => {
    renderDialog();
    expect(screen.getByRole("dialog", { name: "dialog.title" })).toBeVisible();
    expect(screen.getByText('dialog.usage:{"used":1,"quota":4}')).toBeVisible();
  });

  it("shows no denominator for an admin (quota null)", () => {
    renderDialog({
      summary: summary({ quota: null, used: 9, remaining: null }),
    });
    expect(screen.getByText('dialog.usageUnlimited:{"used":9}')).toBeVisible();
    expect(screen.getByRole("button", { name: "dialog.create" })).toBeEnabled();
  });

  it("shows a loader, not an empty list, while the summary is pending", () => {
    renderDialog({ summary: null });
    expect(screen.getByTestId("beta-invite-loading")).toBeInTheDocument();
    expect(screen.queryByText("dialog.list.empty")).toBeNull();
    expect(
      screen.getByRole("button", { name: "dialog.create" }),
    ).toBeDisabled();
  });

  it("reports a failed load inside the dialog", () => {
    renderDialog({ summary: null, error: new Error("boom") });
    expect(screen.getByRole("alert")).toHaveTextContent("dialog.loadFailed");
  });
});

describe("BetaInviteDialog create — one-time URL", () => {
  it("shows the URL once with a working Copy, and forgets it on close", async () => {
    const view = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));

    const field = await screen.findByLabelText("dialog.created.urlLabel");
    expect(field).toHaveValue(INVITE_URL);
    expect(field).toHaveAttribute("readonly");
    expect(screen.getByText("dialog.created.onceNote")).toBeVisible();
    expect(mockCreate).toHaveBeenCalledTimes(1);

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.copy" }),
    );
    await waitFor(() => expect(mockCopyText).toHaveBeenCalledWith(INVITE_URL));
    expect(
      await screen.findByRole("button", { name: "dialog.created.copied" }),
    ).toBeVisible();

    // Close, then reopen: the list is back, the URL is gone for good.
    view.rerender(<BetaInviteDialog {...view.props} open={false} />);
    view.rerender(<BetaInviteDialog {...view.props} open />);
    expect(screen.queryByLabelText("dialog.created.urlLabel")).toBeNull();
    expect(screen.queryByDisplayValue(INVITE_URL)).toBeNull();
    expect(screen.getByRole("button", { name: "dialog.create" })).toBeVisible();
  });

  it("keeps the URL selectable and says so when the clipboard is denied", async () => {
    mockCopyText.mockRejectedValueOnce(new Error("denied"));
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.copy" }),
    );
    expect(
      await screen.findByText("dialog.created.copyFailed"),
    ).toBeVisible();
    expect(screen.getByLabelText("dialog.created.urlLabel")).toHaveValue(
      INVITE_URL,
    );
  });

  it("Done returns to the list without the URL", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.done" }),
    );
    expect(screen.queryByDisplayValue(INVITE_URL)).toBeNull();
    expect(screen.getByRole("button", { name: "dialog.create" })).toBeVisible();
  });
});

describe("BetaInviteDialog at the cap", () => {
  it("disables create and gives the reason", () => {
    renderDialog({ summary: summary({ used: 4, remaining: 0 }) });
    const create = screen.getByRole("button", { name: "dialog.create" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription('dialog.capReached:{"quota":4}');
    fireEvent.click(create);
    expect(mockCreate).not.toHaveBeenCalled();
  });

  it("shows the cap message when the server answers 409 quota_exceeded", async () => {
    mockCreate.mockRejectedValueOnce(
      new ApiError({ message: "quota_exceeded", status: 409 }),
    );
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.quotaExceeded",
    );
    expect(screen.queryByLabelText("dialog.created.urlLabel")).toBeNull();
  });

  it("shows a generic failure for any other create error", async () => {
    mockCreate.mockRejectedValueOnce(
      new ApiError({ message: "boom", status: 500 }),
    );
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.createFailed",
    );
  });
});

describe("BetaInviteDialog invite list", () => {
  const mixed = summary({
    used: 2,
    remaining: 2,
    invites: [
      invite({ id: "inv-active" }),
      invite({
        id: "inv-redeemed",
        status: "redeemed",
        redeemed_at: "2030-01-02T00:00:00Z",
      }),
      invite({ id: "inv-expired", status: "expired" }),
      invite({
        id: "inv-revoked",
        status: "revoked",
        revoked_at: "2030-01-03T00:00:00Z",
      }),
    ],
  });

  it("labels every status in text and never shows a stored URL", () => {
    renderDialog({ summary: mixed });
    for (const status of ["active", "redeemed", "expired", "revoked"]) {
      expect(screen.getByText(`dialog.status.${status}`)).toBeVisible();
    }
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("offers Revoke on active rows only", () => {
    renderDialog({ summary: mixed });
    expect(
      screen.getAllByRole("button", { name: "dialog.list.revoke" }),
    ).toHaveLength(1);
  });

  it("points at the create button when there are no invites yet", () => {
    renderDialog({ summary: summary({ used: 0, remaining: 4, invites: [] }) });
    expect(screen.getByText("dialog.list.empty")).toBeVisible();
  });

  it("revokes only after the confirm", async () => {
    renderDialog({ summary: mixed });
    fireEvent.click(screen.getByRole("button", { name: "dialog.list.revoke" }));
    expect(mockRevoke).not.toHaveBeenCalled();

    const confirm = await screen.findByRole("alertdialog", {
      name: "dialog.revoke.title",
    });
    expect(confirm).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "dialog.revoke.confirm" }),
    );

    await waitFor(() => expect(mockRevoke).toHaveBeenCalledWith("inv-active"));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("cancel leaves the invite alone", async () => {
    renderDialog({ summary: mixed });
    fireEvent.click(screen.getByRole("button", { name: "dialog.list.revoke" }));
    await screen.findByRole("alertdialog");
    fireEvent.click(screen.getByRole("button", { name: "cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(mockRevoke).not.toHaveBeenCalled();
  });

  it("explains a 409 (already used) inside the confirm and keeps it open", async () => {
    mockRevoke.mockRejectedValueOnce(
      new ApiError({ message: "already_redeemed", status: 409 }),
    );
    renderDialog({ summary: mixed });
    fireEvent.click(screen.getByRole("button", { name: "dialog.list.revoke" }));
    await screen.findByRole("alertdialog");
    fireEvent.click(
      screen.getByRole("button", { name: "dialog.revoke.confirm" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.revoke.alreadyRedeemed",
    );
    expect(screen.getByRole("alertdialog")).toBeVisible();
  });
});
