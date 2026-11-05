import { cleanup, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import IntegrationsPage from "./page";

let byok = true;
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => ({ byok }) }));
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: { name: "Studio", current_user_role: "viewer" }, currentWorkspaceId: "studio", workspaces: [{ id: "studio" }] }),
}));
afterEach(cleanup);
beforeEach(() => { byok = true; });

describe("first click from Integrations", () => {
  it("keeps the provider-key entry when the deployment disables BYOK", () => {
    byok = false;
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.provider.title })).toHaveAttribute("href", "/workspace/integrations/external-keys");
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.script.title })).toBeInTheDocument();
  });
  it.each([["en", en, /MCP client setup guide/i, /connect from a script/i, /bring in external data/i],
    ["ja", ja, /MCP クライアント設定ガイド/, /スクリプトから接続/, /外部データを取り込む/]] as const)(
    "leads with the MCP URL and routes starting tasks in %s", (locale, messages, connect, script, ingest) => {
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
      expect(screen.getAllByRole("link")).toHaveLength(8);
    },
  );
});
