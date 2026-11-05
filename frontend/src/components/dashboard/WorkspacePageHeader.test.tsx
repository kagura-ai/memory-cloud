import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspacePageHeader, WorkspacePageNavigationProvider } from "./WorkspacePageHeader";

const state = vi.hoisted(() => ({ path: "/workspace/settings/plan" }));
vi.mock("next/navigation", () => ({ usePathname: () => state.path }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("./WorkspaceSectionNavigation", () => ({
  WorkspaceSectionNavigation: () => <nav aria-label="section tabs">Tabs</nav>,
}));

function view(title = "Feature title") {
  return (
    <WorkspacePageNavigationProvider>
      <WorkspacePageHeader title={title} description="Workspace description" />
    </WorkspacePageNavigationProvider>
  );
}

beforeEach(() => { state.path = "/workspace/settings/plan"; });

describe("workspace page header", () => {
  it("places section navigation after the title and description", () => {
    render(view());
    const heading = screen.getByRole("heading", { name: "workspaceSettings" });
    const description = screen.getByText("Workspace description");
    const tabs = screen.getByRole("navigation", { name: "section tabs" });
    expect(heading.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(description.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it.each([
    "/workspace/integrations",
    "/workspace/integrations/credentials",
    "/workspace/integrations/connectors",
    "/workspace/integrations/external-keys",
    "/workspace/resources",
    "/workspace/secrets",
  ])("keeps the Integrations title on %s", path => {
    state.path = path;
    render(view());
    expect(screen.getByRole("heading", { level: 1, name: "integrations" })).toBeInTheDocument();
    expect(screen.queryByText("Workspace description")).toBeNull();
  });

  it("keeps the title when moving between settings tabs", () => {
    const page = render(view("Plan"));
    expect(screen.getByRole("heading", { name: "workspaceSettings" })).toBeInTheDocument();
    state.path = "/workspace/settings/general";
    page.rerender(view("General"));
    expect(screen.getByRole("heading", { name: "workspaceSettings" })).toBeInTheDocument();
  });

  it.each(["/workspace/resources/resource-id", "/admin/users"])(
    "preserves the page-specific title at %s", path => {
      state.path = path;
      render(view("Detail title"));
      expect(screen.getByRole("heading", { name: "Detail title" })).toBeInTheDocument();
    },
  );

  it("preserves title and actions outside the section provider", () => {
    render(<WorkspacePageHeader title="Standalone" actions={<button>Refresh</button>} />);
    expect(screen.getByRole("heading", { name: "Standalone" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});
