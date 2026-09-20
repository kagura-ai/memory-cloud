/**
 * BetaInviteDialog (#1582): the invite URL is a credential shown exactly once,
 * the create button explains itself at the cap, and revoke is confirmed.
 *
 * #1595: an optional inviter-private label, rows that say who they are for (and
 * who a redeemed one admitted), a one-click reissue that reuses the one-time
 * panel, and a header that separates unused links from real sign-ups.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
import type {
  BetaInvite,
  BetaInviteCreated,
  BetaInviteSummary,
} from "@/lib/api/beta-invites";
import { BetaInviteDialog } from "./BetaInviteDialog";

const INVITE_URL = "https://app.example.com/join/tok_SECRET_once";

const CREATED: BetaInviteCreated = {
  id: "inv-2",
  url: INVITE_URL,
  expires_at: "2030-01-09T00:00:00Z",
  label: null,
};

const invite = (over: Partial<BetaInvite> = {}): BetaInvite => ({
  id: "inv-1",
  status: "active",
  created_at: "2030-01-01T00:00:00Z",
  expires_at: "2030-01-08T00:00:00Z",
  redeemed_at: null,
  revoked_at: null,
  label: null,
  redeemed_email: null,
  ...over,
});

const summary = (over: Partial<BetaInviteSummary> = {}): BetaInviteSummary => ({
  quota: 4,
  used: 1,
  active: 1,
  redeemed: 0,
  remaining: 3,
  invites: [invite()],
  ...over,
});

const conflict = (error: string) =>
  new ApiError({ error, message: error, status: 409 });

const mockCreate = vi.fn();
const mockRevoke = vi.fn();
const mockReissue = vi.fn();

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
    reissue: mockReissue,
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
  mockCreate.mockResolvedValue(CREATED);
  mockRevoke.mockResolvedValue(undefined);
  mockReissue.mockResolvedValue({ ...CREATED, id: "inv-3", label: "Alice" });
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
  it("breaks the usage down into active / redeemed / limit", () => {
    renderDialog({
      summary: summary({ used: 3, active: 2, redeemed: 1, remaining: 1 }),
    });
    expect(screen.getByRole("dialog", { name: "dialog.title" })).toBeVisible();
    // Never the bare `used` (= active + redeemed), which read as "3 signed up".
    expect(
      screen.getByText('dialog.usage:{"active":2,"redeemed":1,"quota":4}'),
    ).toBeVisible();
  });

  it("shows no denominator for an admin (quota null)", () => {
    renderDialog({
      summary: summary({
        quota: null,
        used: 9,
        active: 7,
        redeemed: 2,
        remaining: null,
      }),
    });
    expect(
      screen.getByText('dialog.usageUnlimited:{"active":7,"redeemed":2}'),
    ).toBeVisible();
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

  it("stays open while a create is in flight, so the URL cannot land in a closed dialog", async () => {
    let resolveCreate!: (value: BetaInviteCreated) => void;
    mockCreate.mockReturnValueOnce(
      new Promise<BetaInviteCreated>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    const view = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));

    // Esc and × both ask to close; neither reaches the owner mid-request.
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(view.props.onOpenChange).not.toHaveBeenCalled();

    await act(async () => {
      resolveCreate(CREATED);
    });
    expect(screen.getByLabelText("dialog.created.urlLabel")).toHaveValue(
      INVITE_URL,
    );

    // Settled: closing works again.
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(view.props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("drops a URL that arrives after the owner closed the dialog", async () => {
    let resolveCreate!: (value: BetaInviteCreated) => void;
    mockCreate.mockReturnValueOnce(
      new Promise<BetaInviteCreated>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    const view = renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));

    // The owner controls `open` and can drop it regardless of the guard above.
    view.rerender(<BetaInviteDialog {...view.props} open={false} />);
    await act(async () => {
      resolveCreate(CREATED);
    });

    view.rerender(<BetaInviteDialog {...view.props} open />);
    expect(screen.queryByLabelText("dialog.created.urlLabel")).toBeNull();
    expect(screen.queryByDisplayValue(INVITE_URL)).toBeNull();
    expect(screen.getByRole("button", { name: "dialog.create" })).toBeEnabled();
  });

  it("keeps the URL selectable and says so when the clipboard is denied", async () => {
    mockCopyText.mockRejectedValueOnce(new Error("denied"));
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.copy" }),
    );
    expect(await screen.findByText("dialog.created.copyFailed")).toBeVisible();
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
    // The only text field is the (empty) label input — no row carries a URL.
    const fields = screen.getAllByRole("textbox");
    expect(fields).toHaveLength(1);
    expect(fields[0]).toBe(screen.getByLabelText("dialog.labelField.label"));
    expect(fields[0]).toHaveValue("");
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

describe("BetaInviteDialog label (#1595)", () => {
  it("offers an optional, bounded label field that says who can see it", () => {
    renderDialog();
    const field = screen.getByLabelText("dialog.labelField.label");
    expect(field).toHaveAttribute("maxlength", "100");
    expect(field).toHaveAttribute(
      "placeholder",
      "dialog.labelField.placeholder",
    );
    expect(field).not.toBeRequired();
    expect(field).toHaveAccessibleDescription("dialog.labelField.help");
  });

  it("creates without a label when the field is left empty", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");
    expect(mockCreate).toHaveBeenCalledWith("");
  });

  it("sends the label and clears the field after a successful create", async () => {
    renderDialog();
    fireEvent.change(screen.getByLabelText("dialog.labelField.label"), {
      target: { value: "Alice" },
    });
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");
    expect(mockCreate).toHaveBeenCalledWith("Alice");

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.done" }),
    );
    expect(screen.getByLabelText("dialog.labelField.label")).toHaveValue("");
  });

  it("keeps what was typed when the create fails", async () => {
    mockCreate.mockRejectedValueOnce(
      new ApiError({ message: "boom", status: 500 }),
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText("dialog.labelField.label"), {
      target: { value: "Alice" },
    });
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("dialog.labelField.label")).toHaveValue(
      "Alice",
    );
  });

  it("forgets a half-typed label when the dialog closes", () => {
    const view = renderDialog();
    fireEvent.change(screen.getByLabelText("dialog.labelField.label"), {
      target: { value: "Alice" },
    });
    view.rerender(<BetaInviteDialog {...view.props} open={false} />);
    view.rerender(<BetaInviteDialog {...view.props} open />);
    expect(screen.getByLabelText("dialog.labelField.label")).toHaveValue("");
  });
});

describe("BetaInviteDialog rows (#1595)", () => {
  const LONG = "A very long label ".repeat(5).trim();
  const rows = summary({
    used: 2,
    active: 1,
    redeemed: 1,
    remaining: 2,
    invites: [
      invite({ id: "inv-labelled", label: LONG }),
      invite({ id: "inv-plain" }),
      invite({
        id: "inv-redeemed",
        status: "redeemed",
        redeemed_at: "2030-01-02T00:00:00Z",
        label: "Bob",
        redeemed_email: "bob@invitee.example",
      }),
      invite({
        id: "inv-erased",
        status: "redeemed",
        redeemed_at: "2030-01-02T00:00:00Z",
      }),
      // Not something the API sends; the row must not trust it anyway.
      invite({
        id: "inv-revoked",
        status: "revoked",
        revoked_at: "2030-01-03T00:00:00Z",
        redeemed_email: "ghost@invitee.example",
      }),
    ],
  });

  const row = (id: string) => screen.getByTestId(`beta-invite-row-${id}`);

  it("shows the label, truncated, with the full text in title", () => {
    renderDialog({ summary: rows });
    const label = within(row("inv-labelled")).getByText(LONG);
    expect(label).toHaveAttribute("title", LONG);
    expect(label).toHaveClass("truncate");
  });

  it("shows nothing at all for an unlabelled invite — no filler", () => {
    renderDialog({ summary: rows });
    expect(
      within(row("inv-plain")).queryByTestId("beta-invite-label"),
    ).toBeNull();
    expect(screen.getAllByTestId("beta-invite-label")).toHaveLength(2);
  });

  it("shows who a redeemed invite admitted, with the full address in title", () => {
    renderDialog({ summary: rows });
    const who = within(row("inv-redeemed")).getByText(
      'dialog.list.redeemedBy:{"email":"bob@invitee.example"}',
    );
    expect(who).toHaveAttribute("title", "bob@invitee.example");
    expect(who).toHaveClass("truncate");
  });

  it("shows no address once the account is gone, or on a non-redeemed row", () => {
    renderDialog({ summary: rows });
    expect(screen.getAllByTestId("beta-invite-redeemed-email")).toHaveLength(1);
    expect(screen.queryByText(/ghost@invitee\.example/)).toBeNull();
  });
});

describe("BetaInviteDialog reissue (#1595)", () => {
  const mixed = summary({
    used: 2,
    active: 1,
    redeemed: 1,
    remaining: 2,
    invites: [
      invite({ id: "inv-active", label: "Alice" }),
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

  const rowButton = (id: string, name: string) =>
    within(screen.getByTestId(`beta-invite-row-${id}`)).getByRole("button", {
      name,
    });

  it("offers Reissue on active and expired rows only", () => {
    renderDialog({ summary: mixed });
    expect(
      screen.getAllByRole("button", { name: "dialog.list.reissue" }),
    ).toHaveLength(2);
    expect(rowButton("inv-active", "dialog.list.reissue")).toBeEnabled();
    expect(rowButton("inv-expired", "dialog.list.reissue")).toBeEnabled();
  });

  it("reissues in one click and shows the new URL in the one-time panel", async () => {
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));

    // No confirmation step.
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(mockReissue).toHaveBeenCalledWith("inv-active");

    const field = await screen.findByLabelText("dialog.created.urlLabel");
    expect(field).toHaveValue(INVITE_URL);
    expect(screen.getByText("dialog.created.onceNote")).toBeVisible();
    expect(screen.getByText("dialog.created.reissuedNote")).toBeVisible();
    expect(mockCreate).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "dialog.created.done" }),
    );
    expect(screen.queryByDisplayValue(INVITE_URL)).toBeNull();
    expect(screen.queryByText("dialog.created.reissuedNote")).toBeNull();
  });

  it("a plain create does not claim a previous link stopped working", async () => {
    renderDialog({ summary: mixed });
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");
    expect(screen.queryByText("dialog.created.reissuedNote")).toBeNull();
  });

  it("disables the row while in flight and keeps the dialog open", async () => {
    let resolveReissue!: (value: BetaInviteCreated) => void;
    mockReissue.mockReturnValueOnce(
      new Promise<BetaInviteCreated>((resolve) => {
        resolveReissue = resolve;
      }),
    );
    const view = renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));

    expect(rowButton("inv-active", "dialog.list.reissue")).toBeDisabled();
    expect(rowButton("inv-active", "dialog.list.revoke")).toBeDisabled();
    // One URL panel: a second mint may not race the first.
    expect(rowButton("inv-expired", "dialog.list.reissue")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "dialog.create" }),
    ).toBeDisabled();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(view.props.onOpenChange).not.toHaveBeenCalled();

    await act(async () => {
      resolveReissue(CREATED);
    });
    expect(screen.getByLabelText("dialog.created.urlLabel")).toHaveValue(
      INVITE_URL,
    );
    expect(mockReissue).toHaveBeenCalledTimes(1);
  });

  it("does not replace a URL that is still on screen", async () => {
    renderDialog({ summary: mixed });
    fireEvent.click(screen.getByRole("button", { name: "dialog.create" }));
    await screen.findByLabelText("dialog.created.urlLabel");

    expect(rowButton("inv-active", "dialog.list.reissue")).toBeDisabled();
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    expect(mockReissue).not.toHaveBeenCalled();
  });

  it("drops a URL that arrives after the owner closed the dialog", async () => {
    let resolveReissue!: (value: BetaInviteCreated) => void;
    mockReissue.mockReturnValueOnce(
      new Promise<BetaInviteCreated>((resolve) => {
        resolveReissue = resolve;
      }),
    );
    const view = renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));

    view.rerender(<BetaInviteDialog {...view.props} open={false} />);
    await act(async () => {
      resolveReissue(CREATED);
    });

    view.rerender(<BetaInviteDialog {...view.props} open />);
    expect(screen.queryByDisplayValue(INVITE_URL)).toBeNull();
    expect(rowButton("inv-active", "dialog.list.reissue")).toBeEnabled();
  });

  it("says so when the invite was used meanwhile (409 BETA-INVITE-002)", async () => {
    mockReissue.mockRejectedValueOnce(conflict("BETA-INVITE-002"));
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.reissue.alreadyRedeemed",
    );
    expect(screen.queryByLabelText("dialog.created.urlLabel")).toBeNull();
  });

  it("stays silent on a double-click (409 BETA-INVITE-003): the hook re-read the list", async () => {
    mockReissue.mockRejectedValueOnce(conflict("BETA-INVITE-003"));
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    await waitFor(() =>
      expect(rowButton("inv-active", "dialog.list.reissue")).toBeEnabled(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByLabelText("dialog.created.urlLabel")).toBeNull();
  });

  it("uses the quota message for an expired row at the cap (409 BETA-INVITE-001)", async () => {
    mockReissue.mockRejectedValueOnce(conflict("BETA-INVITE-001"));
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-expired", "dialog.list.reissue"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.quotaExceeded",
    );
    expect(mockReissue).toHaveBeenCalledWith("inv-expired");
  });

  it.each([
    ["a server error", new ApiError({ message: "boom", status: 500 })],
    ["an unknown 409", new ApiError({ message: "?", status: 409 })],
    ["a network failure", new TypeError("Failed to fetch")],
  ])("shows a generic failure for %s", async (_name, failure) => {
    mockReissue.mockRejectedValueOnce(failure);
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "dialog.reissue.failed",
    );
  });

  it("clears a stale reissue error on the next attempt", async () => {
    mockReissue.mockRejectedValueOnce(
      new ApiError({ message: "boom", status: 500 }),
    );
    renderDialog({ summary: mixed });
    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    await screen.findByRole("alert");

    fireEvent.click(rowButton("inv-active", "dialog.list.reissue"));
    await screen.findByLabelText("dialog.created.urlLabel");
    expect(screen.queryByText("dialog.reissue.failed")).toBeNull();
  });
});
