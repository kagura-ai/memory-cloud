import { act, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSectionNavigation } from "./WorkspaceSectionNavigation";

const state = vi.hoisted(() => ({
  path: "/workspace/integrations/credentials",
  query: "tab=api-keys",
  features: { byok: true, plan_page: true } as Record<string, boolean | undefined> | null,
  listKeys: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => state.path,
  useSearchParams: () => new URLSearchParams(state.query),
}));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => state.features }));
vi.mock("@/lib/api/external-keys", () => ({ listExternalAPIKeys: () => state.listKeys() }));

beforeEach(() => {
  state.path = "/workspace/integrations/credentials";
  state.query = "tab=api-keys";
  state.features = { byok: true, plan_page: true };
  state.listKeys.mockReset().mockResolvedValue([{ key_name: "stored" }]);
});

describe("workspace section navigation", () => {
  it("places external API keys last in the requested integration order", () => {
    render(<WorkspaceSectionNavigation />);
    const navigation = screen.getByRole("navigation", { name: "integrations" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map(link => link.textContent)).toEqual([
      "overview", "connectedApps", "apiKeys", "oauthApps", "secrets", "resources", "connectors", "providerKeys",
    ]);
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "apiKeys" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "providerKeys" })).toHaveAttribute(
      "href", "/workspace/integrations/external-keys",
    );
    expect(state.listKeys).not.toHaveBeenCalled();
  });

  it("selects Connected apps without selecting API keys", () => {
    state.query = "tab=connected-apps";
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "connectedApps" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "apiKeys" })).not.toHaveAttribute("aria-current");
  });

  it("selects only external API keys when its page is open", () => {
    state.path = "/workspace/integrations/external-keys";
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "providerKeys" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "apiKeys" })).not.toHaveAttribute("aria-current");
  });

  it.each([
    "/workspace/integrations",
    "/workspace/integrations/connectors",
    "/workspace/resources",
    "/workspace/resources/item",
    "/workspace/secrets",
  ])("shows enabled external API keys from %s", path => {
    state.path = path;
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    expect(state.listKeys).not.toHaveBeenCalled();
  });

  it.each([null, { byok: false }, {}])(
    "keeps the external API key entry regardless of BYOK (%j)",
    async features => {
      state.features = features;
      render(<WorkspaceSectionNavigation />);
      await act(async () => {});
      expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "apiKeys" })).toBeInTheDocument();
      expect(state.listKeys).not.toHaveBeenCalled();
    },
  );

  it("keeps the external-key tab selected when BYOK is disabled", async () => {
    state.path = "/workspace/integrations/external-keys";
    state.features = { byok: false };
    render(<WorkspaceSectionNavigation />);
    await act(async () => {});
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    expect(state.listKeys).not.toHaveBeenCalled();
  });

  it("keeps the entry stable when the deployment flag changes", () => {
    state.features = null;
    const page = render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    state.features = { byok: true };
    page.rerender(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    state.features = { byok: false };
    page.rerender(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
  });

  it.each(["/workspace/resources", "/workspace/resources/item", "/workspace/secrets"])(
    "selects the corresponding integration tab for %s", path => {
      state.path = path;
      render(<WorkspaceSectionNavigation />);
      expect(screen.getByRole("link", {
        name: path.includes("resources") ? "resources" : "secrets",
      })).toHaveAttribute("aria-current", "page");
    },
  );

  it("keeps the settings tab available while the environment hides Plan", () => {
    state.path = "/workspace/settings/general";
    state.features = { plan_page: false };
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "workspaceSettings" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: "plan" })).toBeNull();
    expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
  });
});
