/**
 * #1939: the app-wide paused-features notice — owner-only, off on the plan
 * page (which shows the full banner), silent on a failed fetch.
 */

import { render, screen, waitFor } from "@testing-library/react";
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
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "ws-1",
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

  it("shows nothing when the plan fetch fails", async () => {
    mockGetWorkspacePlan.mockRejectedValue(new Error("boom"));
    const { container } = render(<WorkspaceSuspensionNotice />);
    await waitFor(() => expect(mockGetWorkspacePlan).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
