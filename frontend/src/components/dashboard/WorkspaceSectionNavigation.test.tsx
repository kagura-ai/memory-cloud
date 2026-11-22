import { act, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSectionNavigation } from "./WorkspaceSectionNavigation";

const state = vi.hoisted(() => ({
  path: "/workspace/integrations/credentials",
  query: "tab=api-keys",
  features: { byok: true, plan_page: true } as Record<string, boolean | undefined> | null,
  listKeys: vi.fn(),
  role: "owner" as string | undefined,
  workspaceId: "ws-0",
  seq: 0,
}));
vi.mock("next/navigation", () => ({
  usePathname: () => state.path,
  useSearchParams: () => new URLSearchParams(state.query),
}));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => state.features }));
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { id: state.workspaceId, current_user_role: state.role },
    currentWorkspaceId: state.workspaceId,
  }),
}));
vi.mock("@/lib/api/external-keys", () => ({ listExternalAPIKeys: () => state.listKeys() }));

beforeEach(() => {
  state.path = "/workspace/integrations/credentials";
  state.query = "tab=api-keys";
  state.features = { byok: true, plan_page: true };
  state.role = "owner";
  // The stored-key probe is cached per workspace for the session: a fresh id per test.
  state.seq += 1;
  state.workspaceId = `ws-${state.seq}`;
  state.listKeys.mockReset().mockResolvedValue([{ key_name: "stored" }]);
});

describe("workspace section navigation", () => {
  it.each(["owner", "admin"])("shows history to a workspace %s", role => {
    state.path = "/workspace/settings/history"; state.role = role;
    render(<WorkspaceSectionNavigation />);
    expect(screen.getByRole("link", { name: "operationHistory" })).toHaveAttribute("aria-current", "page");
  });

  it.each(["member", "viewer", undefined])("hides history for role %s", role => {
    state.path = "/workspace/settings/general"; state.role = role;
    render(<WorkspaceSectionNavigation />);
    expect(screen.queryByRole("link", { name: "operationHistory" })).not.toBeInTheDocument();
  });
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

  it("keeps the entry while the deployment flags are still loading", async () => {
    // A momentary /system/info outage is retried and reads as loading (null),
    // not as BYOK off: the entry does not blink away.
    state.features = null;
    render(<WorkspaceSectionNavigation />);
    await act(async () => {});
    expect(screen.getByRole("link", { name: "providerKeys" })).toBeInTheDocument();
    expect(state.listKeys).not.toHaveBeenCalled();
  });

  // #2968: with BYOK off the entry stays only for an owner whose workspace
  // still stores keys (a delete-only list of them).
  it.each([{ byok: false }, {}])(
    "keeps the entry for an owner with stored keys while BYOK is off (%j)",
    async features => {
      state.features = features;
      render(<WorkspaceSectionNavigation />);
      expect(await screen.findByRole("link", { name: "providerKeys" })).toHaveAttribute(
        "href", "/workspace/integrations/external-keys",
      );
      expect(state.listKeys).toHaveBeenCalledTimes(1);
    },
  );

  it.each([{ byok: false }, {}])(
    "hides the entry when BYOK is off and no key is stored (%j)",
    async features => {
      state.features = features;
      state.listKeys.mockResolvedValue([]);
      render(<WorkspaceSectionNavigation />);
      await act(async () => {});
      expect(state.listKeys).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
      expect(screen.getByRole("link", { name: "apiKeys" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "connectors" })).toBeInTheDocument();
    },
  );

  it.each(["admin", "member", "viewer", undefined])(
    "hides the entry from a %s while BYOK is off, without probing the owner-only list",
    async role => {
      state.features = { byok: false };
      state.role = role;
      render(<WorkspaceSectionNavigation />);
      await act(async () => {});
      expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
      expect(state.listKeys).not.toHaveBeenCalled();
    },
  );

  it("hides the entry when the stored-key probe fails while BYOK is off", async () => {
    state.features = { byok: false };
    state.listKeys.mockRejectedValue(new Error("down"));
    render(<WorkspaceSectionNavigation />);
    await act(async () => {});
    expect(screen.queryByRole("link", { name: "providerKeys" })).toBeNull();
  });

  it("keeps the external-key tab selected when BYOK is off and keys are stored", async () => {
    state.path = "/workspace/integrations/external-keys";
    state.features = { byok: false };
    render(<WorkspaceSectionNavigation />);
    expect(await screen.findByRole("link", { name: "providerKeys" })).toHaveAttribute(
      "aria-current", "page",
    );
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
