/**
 * The "Invite a friend" sidebar card (#1582): when it shows, and that × hides
 * it for good. The feature-flag half of the rule lives in the sidebar (no flag
 * → no summary → nothing here); this pins the rest of the matrix.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next-intl", () => ({
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) =>
      vars && Object.keys(vars).length > 0
        ? `${key}:${JSON.stringify(vars)}`
        : key,
}));

import type { BetaInviteSummary } from "@/lib/api/beta-invites";
import {
  BETA_INVITE_CARD_DISMISS_KEY,
  InviteFriendCard,
} from "./InviteFriendCard";

const summary = (over: Partial<BetaInviteSummary> = {}): BetaInviteSummary => ({
  quota: 4,
  used: 1,
  remaining: 3,
  invites: [],
  ...over,
});

beforeEach(() => {
  window.localStorage.clear();
});

describe("InviteFriendCard visibility", () => {
  it("renders nothing while the summary is pending", () => {
    const { container } = render(
      <InviteFriendCard summary={null} onOpen={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows with invites left, and says how many", () => {
    render(<InviteFriendCard summary={summary()} onOpen={vi.fn()} />);
    expect(screen.getByRole("button", { name: /card\.title/ })).toBeVisible();
    expect(screen.getByText('card.remaining:{"remaining":3}')).toBeVisible();
  });

  it("renders nothing at the cap (remaining 0)", () => {
    const { container } = render(
      <InviteFriendCard
        summary={summary({ used: 4, remaining: 0 })}
        onOpen={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("always shows for an admin (remaining null), without a count", () => {
    render(
      <InviteFriendCard
        summary={summary({ quota: null, used: 9, remaining: null })}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /card\.title/ })).toBeVisible();
    expect(screen.queryByText(/card\.remaining/)).toBeNull();
  });

  it("renders nothing once dismissed on an earlier visit", () => {
    window.localStorage.setItem(BETA_INVITE_CARD_DISMISS_KEY, "true");
    const { container } = render(
      <InviteFriendCard summary={summary()} onOpen={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("InviteFriendCard actions", () => {
  it("opens the dialog when the card is clicked", () => {
    const onOpen = vi.fn();
    render(<InviteFriendCard summary={summary()} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: /card\.title/ }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("× hides the card, remembers it, and does not open the dialog", () => {
    const onOpen = vi.fn();
    const { container, unmount } = render(
      <InviteFriendCard summary={summary()} onOpen={onOpen} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "card.dismiss" }));

    expect(container).toBeEmptyDOMElement();
    expect(onOpen).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(BETA_INVITE_CARD_DISMISS_KEY)).toBe(
      "true",
    );

    // "Across reloads": a fresh mount reads the stored flag.
    unmount();
    const again = render(
      <InviteFriendCard summary={summary()} onOpen={onOpen} />,
    );
    expect(again.container).toBeEmptyDOMElement();
  });

  it("uses the documented localStorage key", () => {
    expect(BETA_INVITE_CARD_DISMISS_KEY).toBe(
      "kagura:beta-invite-card-dismissed",
    );
  });
});
