import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import IntegrationsPage from "./page";

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: { name: "Studio", current_user_role: "viewer" } }),
}));
afterEach(cleanup);

describe("first click from Integrations", () => {
  it.each([["en", en, /connect claude/i, /call kmc from a script/i, /bring in external data/i],
    ["ja", ja, /Claudeを接続/, /スクリプトからKMC/, /外部データを取り込む/]] as const)(
    "routes the three starting tasks to their real destinations in %s", (locale, messages, connect, script, ingest) => {
      render(<NextIntlClientProvider locale={locale} messages={messages}><IntegrationsPage /></NextIntlClientProvider>);
      expect(screen.getByText(/Studio/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: connect })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#mcp-connection");
      expect(screen.getByRole("link", { name: script })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#credential-tools");
      expect(screen.getByRole("link", { name: ingest })).toHaveAttribute("href", "/workspace/resources");
      expect(screen.getByText(/setup_resource/)).toBeInTheDocument();
      expect(screen.getAllByRole("link")).toHaveLength(7);
    },
  );
});
