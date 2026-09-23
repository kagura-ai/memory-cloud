/**
 * The blocking terms re-acceptance step (#1665).
 *
 * Pinned: it is a modal dialog with a title, it cannot be dismissed (Escape,
 * no close button), accepting needs the box ticked and posts the current
 * version then refreshes the auth state, and a stale version (409) or a failed
 * request is reported instead of silently closing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ApiError } from "@/lib/api/base";
import { TermsReacceptanceDialog } from "./TermsReacceptanceDialog";

const mockAcceptTerms = vi.fn();
vi.mock("@/lib/auth/auth", () => ({
  acceptTerms: (...args: unknown[]) => mockAcceptTerms(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

const onAccepted = vi.fn();
const onSignOut = vi.fn();

function renderDialog({
  termsVersion = "2026-09",
}: { termsVersion?: string } = {}) {
  return render(
    <TermsReacceptanceDialog
      termsVersion={termsVersion}
      onAccepted={onAccepted}
      onSignOut={onSignOut}
    />,
  );
}

beforeEach(() => {
  mockAcceptTerms.mockReset();
  onAccepted.mockReset();
  onAccepted.mockResolvedValue(undefined);
  onSignOut.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("TermsReacceptanceDialog", () => {
  it("is a titled modal dialog with no close button", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog", { name: "title" });
    expect(dialog).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /close/i })).toBeNull();
  });

  it("cannot be dismissed with Escape", () => {
    renderDialog();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps accept disabled until the terms box is ticked", () => {
    renderDialog();

    const accept = screen.getByRole("button", { name: "accept" });
    expect(accept).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    expect(accept).not.toBeDisabled();
  });

  it("waits for the terms version before it can accept", () => {
    // /system/info has not answered yet.
    render(
      <TermsReacceptanceDialog
        termsVersion={undefined}
        onAccepted={onAccepted}
        onSignOut={onSignOut}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    expect(screen.getByRole("button", { name: "accept" })).toBeDisabled();
  });

  it("posts the version and refreshes the auth state", async () => {
    mockAcceptTerms.mockResolvedValue({
      version: "2026-09",
      recorded: true,
      terms_acceptance_required: false,
    });
    renderDialog();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    fireEvent.click(screen.getByRole("button", { name: "accept" }));

    await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1));
    expect(mockAcceptTerms).toHaveBeenCalledWith("2026-09");
  });

  it("offers a reload when the version went stale (409)", async () => {
    mockAcceptTerms.mockRejectedValue(
      new ApiError({ message: "conflict", status: 409 }),
    );
    renderDialog();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    fireEvent.click(screen.getByRole("button", { name: "accept" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("stale");
    expect(screen.getByRole("button", { name: "reload" })).toBeInTheDocument();
    expect(onAccepted).not.toHaveBeenCalled();
  });

  it("reports any other failure and lets the user retry", async () => {
    mockAcceptTerms.mockRejectedValue(new Error("network"));
    renderDialog();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    fireEvent.click(screen.getByRole("button", { name: "accept" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("failed");
    expect(screen.getByRole("button", { name: "accept" })).not.toBeDisabled();
  });

  it("can sign out instead", () => {
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "signOut" }));

    expect(onSignOut).toHaveBeenCalledTimes(1);
  });
});
