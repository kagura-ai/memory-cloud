/**
 * OverviewTabPanel × NeverDeliveredCard (#3022): the card gets the Memories
 * tab's role check as `canOpenMemories`, and Refresh re-reads its count.
 */

import { forwardRef, useImperativeHandle } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewTabPanel } from "./OverviewTabPanel";
import type { Context } from "@/lib/types/context";

const { cardProps, workspaceRole } = vi.hoisted(() => ({
  cardProps: [] as Array<{ contextId: string; canOpenMemories: boolean; refreshKey: number }>,
  workspaceRole: { current: "admin" as string },
}));

vi.mock("@/components/contexts/NeverDeliveredCard", () => ({
  NeverDeliveredCard: (props: {
    contextId: string;
    canOpenMemories: boolean;
    refreshKey: number;
  }) => {
    cardProps.push(props);
    return (
      <div
        data-testid="nd-card"
        data-can-open={String(props.canOpenMemories)}
        data-refresh={String(props.refreshKey)}
      />
    );
  },
}));

vi.mock("@/components/dashboard/RichMemoryOverview", () => ({
  RichMemoryOverview: forwardRef(function RichMemoryOverview(_props: unknown, ref) {
    useImperativeHandle(ref, () => ({ refresh: async () => {} }));
    return null;
  }),
}));

vi.mock("@/components/dashboard/PublicAPIStats", () => ({
  PublicAPIStats: () => null,
}));

vi.mock("@/lib/api/workspaces", () => ({
  getContextUsageTimeline: vi.fn().mockResolvedValue({ daily_usage: [], total_calls: 0 }),
  getContextUserActivity: vi.fn().mockResolvedValue({ users: [] }),
  getContextPublicAPIStats: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { current_user_role: workspaceRole.current },
    currentWorkspaceId: "ws-1",
  }),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "u", timezone: "UTC" } }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
  useLocale: () => "en",
}));

const CONTEXT = { id: "ctx-1", is_public: false } as unknown as Context;

beforeEach(() => {
  cardProps.length = 0;
  workspaceRole.current = "admin";
});

describe("OverviewTabPanel never-delivered card", () => {
  it("lets an admin open the Memories tab from the card", async () => {
    render(<OverviewTabPanel contextId="ctx-1" context={CONTEXT} />);
    const card = await screen.findByTestId("nd-card");
    expect(card.dataset.canOpen).toBe("true");
    expect(cardProps.at(-1)?.contextId).toBe("ctx-1");
  });

  it("gives a member no link", async () => {
    workspaceRole.current = "member";
    render(<OverviewTabPanel contextId="ctx-1" context={CONTEXT} />);
    expect((await screen.findByTestId("nd-card")).dataset.canOpen).toBe("false");
  });

  it("changes the card's refresh key when Refresh is pressed", async () => {
    render(<OverviewTabPanel contextId="ctx-1" context={CONTEXT} />);
    const card = await screen.findByTestId("nd-card");
    const before = card.dataset.refresh;
    fireEvent.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() =>
      expect(screen.getByTestId("nd-card").dataset.refresh).not.toBe(before),
    );
  });
});
