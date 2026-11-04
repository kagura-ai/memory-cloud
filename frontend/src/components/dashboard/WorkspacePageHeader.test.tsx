import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkspacePageHeader, WorkspacePageNavigationProvider } from "./WorkspacePageHeader";
vi.mock("./WorkspaceSectionNavigation", () => ({ WorkspaceSectionNavigation: () => <nav aria-label="section tabs">Tabs</nav> }));
describe("workspace page header", () => {
  it("places section navigation after the title and description", () => {
    render(<WorkspacePageNavigationProvider><WorkspacePageHeader title="Settings" description="Workspace description" /></WorkspacePageNavigationProvider>);
    const heading = screen.getByRole("heading", { name: "Settings" });
    const description = screen.getByText("Workspace description");
    const tabs = screen.getByRole("navigation", { name: "section tabs" });
    expect(heading.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(description.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
