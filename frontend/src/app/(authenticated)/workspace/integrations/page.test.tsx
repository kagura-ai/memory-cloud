import { act, cleanup, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import IntegrationsPage from "./page";

let byok: boolean | undefined = true;
let role = "viewer";
let workspaceSeq = 0;
let workspaceId = "studio-0";
const listKeys = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/external-keys", () => ({ listExternalAPIKeys: () => listKeys() }));
let currentLocale = "en";
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: currentLocale }) }));
let chatgptAppUrl: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({ byok }),
  useSystemInfo: () => ({ chatgpt_app_url: chatgptAppUrl }),
}));
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: { id: workspaceId, name: "Studio", current_user_role: role }, currentWorkspaceId: workspaceId, workspaces: [{ id: workspaceId }] }),
}));
afterEach(cleanup);
beforeEach(() => {
  byok = true; role = "viewer"; currentLocale = "en"; chatgptAppUrl = null;
  // The stored-key probe is cached per workspace for the session: a fresh id per test.
  workspaceSeq += 1; workspaceId = `studio-${workspaceSeq}`;
  listKeys.mockReset().mockResolvedValue([{ key_name: "OPENAI_API_KEY" }]);
});

describe("first click from Integrations", () => {
  // #2968: with BYOK off the provider-key entry stays only for an owner whose
  // workspace still stores keys.
  it.each([false, undefined])("drops the provider-key entry when BYOK is off (%s) and no key is stored", async off => {
    byok = off; role = "owner";
    listKeys.mockResolvedValue([]);
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    await act(async () => {});
    expect(listKeys).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("link", { name: en.integrationEntry.tasks.provider.title })).toBeNull();
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.script.title })).toBeInTheDocument();
  });
  it("keeps the provider-key entry for an owner with stored keys while BYOK is off", async () => {
    byok = false; role = "owner";
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    expect(await screen.findByRole("link", { name: en.integrationEntry.tasks.provider.title })).toHaveAttribute("href", "/workspace/integrations/external-keys");
  });
  it.each(["admin", "member", "viewer"])("drops the provider-key entry for a %s while BYOK is off, without probing", async who => {
    byok = false; role = who;
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    await act(async () => {});
    expect(screen.queryByRole("link", { name: en.integrationEntry.tasks.provider.title })).toBeNull();
    expect(listKeys).not.toHaveBeenCalled();
  });
  it("keeps the provider-key entry with BYOK on, without probing", () => {
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.provider.title })).toHaveAttribute("href", "/workspace/integrations/external-keys");
    expect(listKeys).not.toHaveBeenCalled();
  });
  it.each([["en", en, /MCP client setup guide/i, /connect from a script/i, /bring in external data/i],
    ["ja", ja, /MCP クライアント設定ガイド/, /スクリプトから接続/, /外部データを取り込む/]] as const)(
    "leads with the MCP URL and routes starting tasks in %s", (locale, messages, connect, script, ingest) => {
      currentLocale = locale;
      render(<NextIntlClientProvider locale={locale} messages={messages}><IntegrationsPage /></NextIntlClientProvider>);
      expect(screen.getByText(/Studio/)).toBeInTheDocument();
      const mcpHeading = screen.getByRole("heading", { name: messages.mcpConnection.quickStart.title });
      const tasksHeading = screen.getByRole("heading", { name: messages.integrationEntry.start });
      expect(mcpHeading.compareDocumentPosition(tasksHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(screen.getByRole("button", { name: messages.mcpConnection.copyUrl })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: connect })).toHaveAttribute("href", "https://github.com/kagura-ai/memory-cloud/blob/main/docs/mcp-clients.md");
      expect(screen.getByRole("link", { name: script })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#credential-tools");
      expect(screen.getByRole("link", { name: ingest })).toHaveAttribute("href", "/workspace/resources");
      expect(screen.getByText(/Gemini.*Muse/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: messages.integrationEntry.tasks.connector.title })).toHaveAttribute("href", "/workspace/integrations/connectors");
      expect(screen.getByText(/setup_resource/)).toBeInTheDocument();
      const tasks = within(screen.getByRole("region", { name: messages.integrationEntry.start })).getAllByRole("link");
      expect(tasks.map(link => link.getAttribute("href"))).toEqual([
        "/workspace/integrations/credentials?tab=connected-apps",
        "/workspace/integrations/credentials?tab=api-keys#credential-tools",
        "/workspace/integrations/credentials?tab=oauth-apps#credential-tools",
        "/workspace/secrets",
        "/workspace/resources",
        "/workspace/integrations/connectors",
        "/workspace/integrations/external-keys",
      ]);
      expect(within(tasks[0]).getByText(messages.sectionNavigation.connectedApps)).toBeInTheDocument();
      expect(within(tasks[1]).getByText(messages.sectionNavigation.apiKeys)).toBeInTheDocument();
      expect(tasks[1]).toContainElement(screen.getByText(messages.integrationEntry.tasks.script.description));
      expect(tasks[1]).toHaveClass("focus-visible:ring-2", "hover:shadow-sm");
      expect(screen.getByRole("link", { name: `${messages.pluginGuide.label} ${messages.pluginGuide.newTab.trim()}` })).toHaveAttribute("href", `https://www.kagura-ai.com${locale === "ja" ? "/ja" : ""}/help/guide/#plugins`);
      expect(screen.getAllByRole("link")).toHaveLength(9);
    },
  );
  it.each([["en", en], ["ja", ja]] as const)("links the deployment's ChatGPT app next to the plugin guide in %s (#2760)", (locale, messages) => {
    currentLocale = locale;
    chatgptAppUrl = "https://example.com/chatgpt-app";
    render(<NextIntlClientProvider locale={locale} messages={messages}><IntegrationsPage /></NextIntlClientProvider>);
    const link = screen.getByRole("link", { name: `${messages.chatgptApp.label} ${messages.chatgptApp.newTab.trim()}` });
    expect(link).toHaveAttribute("href", "https://example.com/chatgpt-app");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(messages.chatgptApp.description)).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(10);
  });
});
