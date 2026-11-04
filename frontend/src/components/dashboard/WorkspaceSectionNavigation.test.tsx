import { render, screen, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSectionNavigation } from "./WorkspaceSectionNavigation";

const state = vi.hoisted(() => ({
  path: "/workspace/integrations/credentials",
  query: "tab=api-keys",
  features: { byok: true, plan_page: true } as Record<string, boolean | undefined> | null,
  role: "owner",
  workspaceId: "one",
  listKeys: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => state.path,
  useSearchParams: () => new URLSearchParams(state.query),
}));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => state.features }));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({
  currentWorkspace: { id: state.workspaceId, current_user_role: state.role },
}) }));
vi.mock("@/lib/api/external-keys", () => ({ listExternalAPIKeys: () => state.listKeys() }));

let scope = 0;
beforeEach(() => {
  state.path = "/workspace/integrations/credentials"; state.query = "tab=api-keys";
  state.features = { byok: true, plan_page: true }; state.role = "owner";
  state.workspaceId = "nav-test-" + ++scope;
  state.listKeys.mockReset().mockResolvedValue([]);
});
describe("workspace section navigation", () => {
  it("keeps the main integration destinations and selects the API-key group", () => {
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "apiKeys" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "kmcKeys" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "connectors" })).toHaveAttribute("href", "/workspace/integrations/connectors");
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    expect(state.listKeys).not.toHaveBeenCalled();
  });
  it("selects external keys under the same API-key destination", () => {
    state.path = "/workspace/integrations/external-keys";
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "apiKeys" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "providerKeys" })).toHaveAttribute("aria-current", "page");
  });
  it.each([null, { byok: false }, {}])("hides provisioning for unresolved or disabled flags (%j)", async features => {
    state.features = features;
    render(<WorkspaceSectionNavigation />);
    await act(async () => {});
    expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
    expect(screen.getByRole("link", { name: "kmcKeys" })).toBeInTheDocument();
  });
  it("preserves the owner's console for keys stored before BYOK was disabled", async () => {
    state.features = { byok: false }; state.listKeys.mockResolvedValue([{ key_name: "stored" }]);
    render(<WorkspaceSectionNavigation />);
    expect(await screen.findByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    expect(state.listKeys).toHaveBeenCalledTimes(1);
  });
  it.each(["admin", "member", "viewer"])("never probes the owner-only key list for %s", async role => {
    state.role = role; state.features = { byok: false };
    render(<WorkspaceSectionNavigation />); await act(async () => {});
    expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
    expect(state.listKeys).not.toHaveBeenCalled();
  });
  it("retries a failed old-key lookup on the next mount without claiming it is empty", async () => {
    state.features = { byok: false }; state.listKeys.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce([{ key_name: "stored" }]);
    const first = render(<WorkspaceSectionNavigation />); await waitFor(() => expect(state.listKeys).toHaveBeenCalledTimes(1)); first.unmount();
    render(<WorkspaceSectionNavigation />); expect(await screen.findByRole("link", { name: "providerKeys" })).toBeInTheDocument();
  });
  it("does not reuse a previous workspace's key presence", async () => {
    state.features = { byok: false }; state.listKeys.mockResolvedValueOnce([{ key_name: "stored" }]);
    const page = render(<WorkspaceSectionNavigation />); await screen.findByRole("link", { name: "providerKeys" });
    state.workspaceId = "new-" + state.workspaceId; page.rerender(<WorkspaceSectionNavigation />);
    expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
  });
  it.each(["/workspace/resources", "/workspace/resources/item", "/workspace/secrets"])("shows the integration tabs for %s", path => {
    state.path = path; render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("navigation", { name: "integrations" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: path.includes("resources") ? "resources" : "secrets" })).toHaveAttribute("aria-current", "page");
  });
  it("keeps the settings tab available while the environment hides Plan", () => {
    state.path = "/workspace/settings/general"; state.features = { plan_page: false };
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "workspaceSettings" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: "plan" })).toBeNull();
  });
});
