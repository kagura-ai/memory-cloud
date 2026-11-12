/**
 * /email/unsubscribe#<token> (#2761): token read from the fragment and removed
 * from the address bar, nothing posted until the button is pressed, the same
 * "done" message for every outcome, retry messages for 429 / failures.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockUnsubscribe = vi.fn();
vi.mock("@/lib/api/announcementEmails", () => ({
  unsubscribeAnnouncementEmails: (...args: unknown[]) =>
    mockUnsubscribe(...args),
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

vi.mock("next-intl", () => ({ useTranslations: () => (k: string) => k }));

import UnsubscribePage from "./page";

const TOKEN = "gAAAAABtokenvalue_-123";

beforeEach(() => {
  mockUnsubscribe.mockReset();
  window.history.replaceState(null, "", `/email/unsubscribe#${TOKEN}`);
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("/email/unsubscribe", () => {
  it("reads the token from the fragment, clears it and waits for the button", async () => {
    render(<UnsubscribePage />);

    const button = await screen.findByTestId("unsubscribe-button");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/email/unsubscribe");
    expect(mockUnsubscribe).not.toHaveBeenCalled();

    fireEvent.click(button);
    await waitFor(() => expect(mockUnsubscribe).toHaveBeenCalledWith(TOKEN));
    expect(await screen.findByTestId("unsubscribe-done")).toHaveTextContent(
      "done",
    );
    expect(screen.queryByTestId("unsubscribe-button")).toBeNull();
  });

  it("shows the same done message whatever the backend decided", async () => {
    mockUnsubscribe.mockResolvedValue({ status: "accepted" });
    render(<UnsubscribePage />);
    fireEvent.click(await screen.findByTestId("unsubscribe-button"));
    expect(await screen.findByText("done")).toBeInTheDocument();
    expect(screen.getByText("doneTitle")).toBeInTheDocument();
  });

  it("says the link is incomplete without a token", async () => {
    window.history.replaceState(null, "", "/email/unsubscribe");
    render(<UnsubscribePage />);
    expect(await screen.findByText("invalidLink")).toBeInTheDocument();
    expect(screen.queryByTestId("unsubscribe-button")).toBeNull();
  });

  it("asks to retry later on 429", async () => {
    mockUnsubscribe.mockRejectedValue(new FakeApiError(429));
    render(<UnsubscribePage />);
    fireEvent.click(await screen.findByTestId("unsubscribe-button"));
    expect(await screen.findByText("rateLimited")).toBeInTheDocument();
    expect(screen.getByTestId("unsubscribe-button")).not.toBeDisabled();
  });

  it("shows an error on failure and keeps the button", async () => {
    mockUnsubscribe.mockRejectedValue(new FakeApiError(503));
    render(<UnsubscribePage />);
    fireEvent.click(await screen.findByTestId("unsubscribe-button"));
    expect(await screen.findByText("error")).toBeInTheDocument();
    expect(screen.getByTestId("unsubscribe-button")).toBeInTheDocument();
  });

  it("links to the account settings", async () => {
    render(<UnsubscribePage />);
    expect(screen.getByRole("link", { name: "manage" })).toHaveAttribute(
      "href",
      "/profile#profile-emails",
    );
  });
});
