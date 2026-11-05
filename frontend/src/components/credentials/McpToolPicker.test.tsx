import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpToolPicker } from "./McpToolPicker";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/base", () => ({ apiClient: { get } }));
vi.mock("next-intl", () => ({ useTranslations: () => Object.assign((key: string, values?: { name: string }) => values ? key + " " + values.name : key, { has: () => false }) }));
const tools = [
  { name: "explore", description: "Explore related memories" },
  { name: "setup_resource", description: "Create a resource" },
];
beforeEach(() => { get.mockReset().mockResolvedValue({ tools }); });
function Picker() {
  const [value, setValue] = useState<string[]>([]);
  return <McpToolPicker value={value} onChange={setValue} />;
}
describe("MCP additional tool picker", () => {
  it("selects suggestions as removable tags and filters the candidates", async () => {
    render(<Picker />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /explore/ }));
    expect(screen.getByRole("button", { name: "remove explore" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "resource" } });
    expect(screen.queryByRole("checkbox", { name: /explore/ })).toBeNull();
    expect(screen.getByRole("checkbox", { name: /setup_resource/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "remove explore" }));
    expect(screen.queryByRole("list", { name: "selected" })).toBeNull();
  });
  it("shows selected presets and toggles their tags together", async () => {
    render(<Picker />);
    const preset = screen.getByRole("button", { name: "preset.ingestion" });
    await waitFor(() => expect(preset).toBeEnabled());
    fireEvent.click(preset);
    expect(preset).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "remove setup_resource" })).toBeInTheDocument();
    fireEvent.click(preset);
    expect(preset).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("button", { name: "remove setup_resource" })).toBeNull();
  });
  it("disables the selected tags and choices in full mode", async () => {
    render(<McpToolPicker value={["explore"]} onChange={vi.fn()} disabled />);
    expect(await screen.findByRole("checkbox", { name: /explore/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "remove explore" })).toBeDisabled();
    expect(screen.getByRole("searchbox")).toBeDisabled();
  });
  it("shows a retry after a failed catalog lookup", async () => {
    get.mockRejectedValueOnce(new Error("offline"));
    render(<Picker />);
    expect(await screen.findByRole("alert")).toHaveTextContent("loadError");
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(await screen.findByRole("checkbox", { name: /setup_resource/ })).toBeInTheDocument();
  });
});
