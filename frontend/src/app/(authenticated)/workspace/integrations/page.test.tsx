import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import IntegrationsPage from "./page";

let byok = true;
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => ({ byok }) }));
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: { name: "Studio", current_user_role: "viewer" } }),
}));
afterEach(cleanup);
beforeEach(() => { byok = true; });

describe("first click from Integrations", () => {
  it("hides the provider-key entry when the deployment disables BYOK", () => {
    byok = false;
    render(<NextIntlClientProvider locale="en" messages={en}><IntegrationsPage /></NextIntlClientProvider>);
    expect(screen.queryByRole("link", { name: en.integrationEntry.tasks.provider.title })).toBeNull();
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.script.title })).toBeInTheDocument();
  });
  it.each([["en", en, /connect an ai client/i, /call kmc from a script/i, /bring in external data/i],
    ["ja", ja, /AIクライアントを接続/, /スクリプトからKMC/, /外部データを取り込む/]] as const)(
    "routes the three starting tasks to their real destinations in %s", (locale, messages, connect, script, ingest) => {
      render(<NextIntlClientProvider locale={locale} messages={messages}><IntegrationsPage /></NextIntlClientProvider>);
      expect(screen.getByText(/Studio/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: connect })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#mcp-connection");
      expect(screen.getByRole("link", { name: script })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#credential-tools");
      expect(screen.getByRole("link", { name: ingest })).toHaveAttribute("href", "/workspace/resources");
      expect(screen.getByText(/Gemini.*Muse/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: messages.integrationEntry.tasks.connector.title })).toHaveAttribute("href", "/workspace/integrations/connectors");
      expect(screen.getByText(/setup_resource/)).toBeInTheDocument();
      expect(screen.getAllByRole("link")).toHaveLength(8);
    },
  );
});
