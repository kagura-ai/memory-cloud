import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import ja from "@/messages/ja.json";
import en from "@/messages/en.json";
import { MCP_TOOL_PRESETS } from "@/lib/mcp/toolPresets";
import { McpToolPicker } from "./McpToolPicker";

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ tools: [
    { name: "explore", description: "Find related memories" },
    { name: "setup_resource", description: "Create an ingestion resource" },
  ] }) },
}));
describe("localized MCP tool choices", () => {
  it("finds tools by Japanese descriptions and uses canonical names for the selection", async () => {
    const change = vi.fn();
    render(<NextIntlClientProvider locale="ja" messages={ja}><McpToolPicker value={[]} onChange={change} /></NextIntlClientProvider>);
    await screen.findByRole("checkbox", { name: /関連する記憶/ });
    fireEvent.change(screen.getByRole("searchbox", { name: "ツールを検索" }), { target: { value: "取り込み" } });
    expect(screen.queryByRole("checkbox", { name: /関連する記憶/ })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /リソースを作成/ }));
    expect(change).toHaveBeenCalledWith(["setup_resource"]);
  });

  it("has bilingual descriptions for every preset tool", () => {
    expect(Object.keys(ja.mcpToolDescriptions).sort()).toEqual(Object.keys(en.mcpToolDescriptions).sort());
    for (const preset of MCP_TOOL_PRESETS) {
      for (const name of preset.tools) {
        expect(ja.mcpToolDescriptions[name].title.trim()).not.toBe("");
        expect(en.mcpToolDescriptions[name].description.trim()).not.toBe("");
      }
    }
  });
});
