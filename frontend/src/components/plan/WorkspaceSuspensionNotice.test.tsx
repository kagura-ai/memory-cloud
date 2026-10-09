/**
 * #1939: the app-wide paused-features notice — owner-only, off on the plan
 * page (which shows the full banner), silent on a failed fetch.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceSuspensionNotice } from "./WorkspaceSuspensionNotice";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
  }: {
    href: string;
    children: React.ReactNode;
  }) => <a href={href}>{children}</a>,
}));
let mockPathname = "/dashboard";
vi.mock("next/navigation", () => ({ usePathname: () => mockPathname }));
let mockRole = "owner";
let mockWorkspaceId = "ws-1";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: mockWorkspaceId,
    currentWorkspace: { current_user_role: mockRole },
  }),
}));
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({ plan_page: true }),
}));
const mockGetWorkspacePlan = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspacePlan: (...args: unknown[]) => mockGetWorkspacePlan(...args),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mockPathname = "/dashboard";
  mockRole = "owner";
  mockWorkspaceId = "ws-1";
  mockGetWorkspacePlan.mockResolvedValue({ suspended: ["sleep"] });
});

describe("WorkspaceSuspensionNotice", () => {
  it("shows the owner the paused features with a link to the plan page", async () => {
    render(<WorkspaceSuspensionNotice />);
    expect(
      await screen.findByText("planPage.suspended.features.sleep"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/workspace/settings/plan",
    );
    expect(mockGetWorkspacePlan).toHaveBeenCalledWith("ws-1");
  });

  it("never fetches the owner-only plan for a non-owner", () => {
    mockRole = "member";
    const { container } = render(<WorkspaceSuspensionNotice />);
    expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("stays out of the plan page, which renders the full banner", () => {
    mockPathname = "/workspace/settings/plan";
    const { container } = render(<WorkspaceSuspensionNotice />);
    expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("re-reads the plan on navigation while shown, so it clears after re-subscribing", async () => {
    const { rerender } = render(<WorkspaceSuspensionNotice />);
    expect(
      await screen.findByText("planPage.suspended.features.sleep"),
    ).toBeInTheDocument();
    // No second fetch just because the notice appeared.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockGetWorkspacePlan).toHaveBeenCalledTimes(1);

    mockGetWorkspacePlan.mockResolvedValue({ suspended: [] });
    mockPathname = "/memories";
    rerender(<WorkspaceSuspensionNotice />);

    await waitFor(() =>
      expect(
        screen.queryByText("planPage.suspended.features.sleep"),
      ).not.toBeInTheDocument(),
    );
    expect(mockGetWorkspacePlan).toHaveBeenCalledTimes(2);
  });

  it("still shows under StrictMode's double effect", async () => {
    render(
      <StrictMode>
        <WorkspaceSuspensionNotice />
      </StrictMode>,
    );
    expect(
      await screen.findByText("planPage.suspended.features.sleep"),
    ).toBeInTheDocument();
  });

  it("does not re-read on navigation when nothing is paused", async () => {
    mockGetWorkspacePlan.mockResolvedValue({ suspended: [] });
    const { rerender } = render(<WorkspaceSuspensionNotice />);
    await waitFor(() => expect(mockGetWorkspacePlan).toHaveBeenCalledTimes(1));

    mockPathname = "/memories";
    rerender(<WorkspaceSuspensionNotice />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockGetWorkspacePlan).toHaveBeenCalledTimes(1);
  });

  it("shows nothing when the plan fetch fails", async () => {
    mockGetWorkspacePlan.mockRejectedValue(new Error("boom"));
    const { container } = render(<WorkspaceSuspensionNotice />);
    await waitFor(() => expect(mockGetWorkspacePlan).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("drops the previous workspace's list at once on a workspace switch", async () => {
    const { rerender } = render(<WorkspaceSuspensionNotice />);
    expect(
      await screen.findByText("planPage.suspended.features.sleep"),
    ).toBeInTheDocument();
    // Workspace B's read never answers: the stale list must not linger.
    mockGetWorkspacePlan.mockReturnValue(new Promise(() => {}));
    mockWorkspaceId = "ws-2";
    rerender(<WorkspaceSuspensionNotice />);
    await waitFor(() =>
      expect(
        screen.queryByText("planPage.suspended.features.sleep"),
      ).not.toBeInTheDocument(),
    );
    expect(mockGetWorkspacePlan).toHaveBeenLastCalledWith("ws-2");
  });
});
