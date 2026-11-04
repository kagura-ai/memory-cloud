import { useEffect } from "react";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FeatureRouteBoundary } from "./FeatureRouteBoundary";
import en from "@/messages/en.json";

const fixture = vi.hoisted(() => ({
  path: "/workspace/resources",
  role: "member",
  systemRole: "user",
  loading: false,
  flags: { plan_page: true, byok: true, cost_display: true },
  planExcluded: true,
  lookupFailed: false,
  request: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => fixture.path,
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user: { role: fixture.systemRole } }) }));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({
  currentWorkspace: { id: "selected", name: "Selected workspace", plan_name: "sm", current_user_role: fixture.role },
  currentWorkspaceId: "selected", loading: fixture.loading,
}) }));
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => ({ features: fixture.flags }),
  useSystemFeatures: () => fixture.flags,
  isSystemInfoUnavailable: () => fixture.lookupFailed,
}));
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => [
    { name: "sm", resources: !fixture.planExcluded, sleep_enabled_contexts_limit: 0 },
    { name: "xl", resources: true, sleep_enabled_contexts_limit: 2, display_name: "XL" },
  ],
}));
function ProtectedPage() {
  useEffect(() => { fixture.request(); }, []);
  return <p>protected contents</p>;
}
function view() {
  return <NextIntlClientProvider locale="en" messages={en}><FeatureRouteBoundary><ProtectedPage /></FeatureRouteBoundary></NextIntlClientProvider>;
}
beforeEach(() => {
  fixture.path = "/workspace/resources"; fixture.role = "member"; fixture.systemRole = "user";
  fixture.loading = false; fixture.planExcluded = true; fixture.lookupFailed = false; fixture.flags.plan_page = true;
  fixture.request.mockClear();
});
describe("feature routes", () => {
  it("explains both resource restrictions without loading a protected list or enabling billing", () => {
    render(view());
    expect(screen.getByRole("heading", { level: 1, name: en.sidebar.resources })).toBeInTheDocument();
    expect(screen.getByText(/Only the workspace owner/i)).toBeInTheDocument();
    expect(screen.getByText("New resources require the XL plan")).toBeInTheDocument();
    expect(fixture.request).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /upgrade/i })).toBeNull();
  });
  it("shows the independent sleep plan limit to a viewer without loading reports", () => {
    fixture.path = "/workspace/sleep-reports"; fixture.role = "viewer"; render(view());
    expect(screen.getByText(en.featureAccess.admin)).toBeInTheDocument();
    expect(screen.getAllByText(/XL/i).length).toBeGreaterThan(0);
    expect(fixture.request).not.toHaveBeenCalled();
  });
  it.each(["/admin/users/any-id", "/admin/sleep-reports/not-an-id"])(
    "workspace owners never load system admin details at %s", (path) => {
      fixture.path = path; fixture.role = "owner";
      render(view());
      expect(screen.getByText(/system administrator/i)).toBeInTheDocument();
      expect(fixture.request).not.toHaveBeenCalled();
    },
  );
  it("preserves the page for authorized lower-tier users with existing objects", () => {
    fixture.role = "owner"; render(view());
    expect(screen.getByText("protected contents")).toBeInTheDocument();
  });
  it("explains deployment-disabled Plan and distinguishes a failed feature lookup", () => {
    fixture.path = "/workspace/settings/plan"; fixture.role = "owner"; fixture.flags.plan_page = false;
    const { rerender } = render(view());
    expect(screen.getByText(en.featureAccess.deployment)).toBeInTheDocument();
    expect(fixture.request).not.toHaveBeenCalled();
    fixture.lookupFailed = true; rerender(view());
    expect(screen.getByRole("button", { name: en.featureAccess.retry })).toBeInTheDocument();
    expect(screen.queryByText(en.featureAccess.deployment)).toBeNull();
    expect(fixture.request).not.toHaveBeenCalled();
  });
  it("withholds the plan child when switching from an owned to a joined workspace", () => {
    fixture.path = "/workspace/settings/plan"; fixture.role = "owner";
    const { rerender } = render(view()); fixture.request.mockClear();
    fixture.loading = true; rerender(view());
    expect(screen.queryByText("protected contents")).toBeNull();
    fixture.loading = false; fixture.role = "viewer"; rerender(view());
    expect(screen.queryByText("protected contents")).toBeNull();
    expect(fixture.request).not.toHaveBeenCalled();
  });
});
