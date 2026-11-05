import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CredentialsPage from "./page";

const state = vi.hoisted(() => ({ tab: "api-keys", setTab: vi.fn(), replace: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: state.replace }) }));
vi.mock("@/hooks/useTabParam", () => ({ useTabParam: () => [state.tab, state.setTab] }));
vi.mock("@/components/credentials/APIKeysTabPanel", () => ({
  APIKeysTabPanel: () => <div>API key list</div>,
}));
vi.mock("@/components/credentials/OAuthAppsTabPanel", () => ({
  OAuthAppsTabPanel: () => <div>OAuth client list</div>,
}));
vi.mock("@/components/credentials/ConnectedAppsSection", () => ({
  ConnectedAppsSection: () => <div>Connected app list</div>,
}));

beforeEach(() => {
  state.tab = "api-keys";
  state.setTab.mockClear();
  state.replace.mockClear();
  window.history.replaceState(null, "", "/workspace/integrations/credentials?tab=api-keys");
});
afterEach(() => window.history.replaceState(null, "", "/"));

describe("focused credential pages", () => {
  it.each([
    ["api-keys", "API key list"],
    ["oauth-apps", "OAuth client list"],
    ["connected-apps", "Connected app list"],
  ])("shows only the selected %s panel", (tab, contents) => {
    state.tab = tab;
    render(<CredentialsPage />);
    expect(screen.getByText(contents)).toBeInTheDocument();
    for (const other of ["API key list", "OAuth client list", "Connected app list"]) {
      if (other !== contents) expect(screen.queryByText(other)).toBeNull();
    }
    expect(screen.queryByText("connectionGuide")).toBeNull();
  });

  it("routes old MCP setup links to the overview", () => {
    window.history.replaceState(null, "", "/workspace/integrations/credentials?tab=api-keys#mcp-connection");
    render(<CredentialsPage />);
    expect(state.replace).toHaveBeenCalledWith("/workspace/integrations#mcp-connection");
  });

  it("preserves the former connected-apps anchor", () => {
    window.history.replaceState(null, "", "/workspace/integrations/credentials?tab=api-keys#connected-apps");
    render(<CredentialsPage />);
    expect(state.setTab).toHaveBeenCalledWith("connected-apps");
  });

  it("routes an MCP setup anchor opened on the current page", () => {
    render(<CredentialsPage />);
    window.history.replaceState(null, "", "/workspace/integrations/credentials?tab=api-keys#mcp-connection");
    fireEvent(window, new Event("hashchange"));
    expect(state.replace).toHaveBeenCalledWith("/workspace/integrations#mcp-connection");
  });
});
