import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { GettingStarted } from "./GettingStarted";

const recall = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/memory", () => ({ recallMemories: recall }));
const contexts = [{ id: "c1", name: "Team", display_name: null }, { id: "c2", name: "Launch", display_name: null }];
function guide(scope: string, locale: "en" | "ja" = "en", options = contexts) {
  return <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ja}>
    <GettingStarted key={scope} scope={scope} owner={false} contexts={options} />
  </NextIntlClientProvider>;
}
beforeEach(() => { localStorage.clear(); recall.mockReset(); });
describe("getting started for an invited workspace member", () => {
  it("recalls only in the selected accessible context without a practice write", async () => {
    recall.mockResolvedValue({ results: [{ memory_id: "m1", summary: "Decided on Friday" }] });
    render(guide("a", "ja"));
    fireEvent.click(screen.getByRole("combobox", { name: ja.gettingStarted.choose }));
    fireEvent.click(await screen.findByRole("option", { name: "Launch" }));
    fireEvent.change(screen.getByLabelText(ja.gettingStarted.queryLabel), { target: { value: "when" } });
    fireEvent.click(screen.getByRole("button", { name: ja.gettingStarted.find }));
    expect(await screen.findByText("Decided on Friday")).toBeInTheDocument();
    expect(recall).toHaveBeenCalledWith({ query: "when", k: 3, filters: { context_id: "c2" } });
    expect(screen.queryByRole("button", { name: ja.gettingStarted.practice })).toBeNull();
    fireEvent.click(screen.getByRole("combobox", { name: ja.gettingStarted.choose }));
    fireEvent.click(await screen.findByRole("option", { name: "Team" }));
    expect(screen.queryByText("Decided on Friday")).toBeNull();
  });
  it("scopes skip and replay to the current user and workspace", async () => {
    const page = render(guide("u1:ws1"));
    fireEvent.click(screen.getByText(en.gettingStarted.skip));
    expect(screen.getByRole("button", { name: en.gettingStarted.title })).toHaveFocus();
    page.rerender(guide("u2:ws1")); expect(screen.getByText(en.gettingStarted.intro)).toBeInTheDocument();
    page.rerender(guide("u1:ws2")); expect(screen.getByText(en.gettingStarted.intro)).toBeInTheDocument();
    page.rerender(guide("u1:ws1")); expect(screen.queryByText(en.gettingStarted.intro)).toBeNull();
    fireEvent.click(screen.getByRole("button", {name: en.gettingStarted.title}));
    await waitFor(() => expect(screen.getByText(en.gettingStarted.intro)).toBeInTheDocument());
  });
  it("offers connection or access guidance when no context is available", () => {
    render(guide("u:empty", "en", []));
    expect(screen.getByText(en.gettingStarted.noContexts)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: en.integrationEntry.tasks.client.title })).toHaveAttribute("href", "/workspace/integrations/credentials?tab=api-keys#mcp-connection");
    expect(screen.queryByRole("button", { name: en.gettingStarted.find })).toBeNull();
    expect(recall).not.toHaveBeenCalled();
  });
});
