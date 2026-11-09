import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Page from "./page";
import { HistoryView } from "./HistoryView";
import type { HistoryPage } from "@/lib/api/operation-history";

const state = vi.hoisted(() => ({
  get: vi.fn(), gate: "allowed", errorGate: false,
  workspace: { id: "workspace-one", current_user_role: "owner", plan_name: "promax" },
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => values ? `${key}:${Object.values(values).join(",")}` : key,
}));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({ currentWorkspace: state.workspace }) }));
vi.mock("@/hooks/useFeatureGate", () => ({ useFeatureGate: () => ({ state: state.gate }) }));
vi.mock("@/hooks/useErrorGate", () => ({ useErrorGate: (error: unknown) => error && state.errorGate ? { state: "plan" } : null }));
vi.mock("@/components/common/FeatureGateNotice", () => ({ FeatureGateNotice: ({ gate }: { gate: { state: string } }) => <p>gate:{gate.state}</p> }));
vi.mock("@/components/dashboard/WorkspacePageHeader", () => ({ WorkspacePageHeader: ({ title }: { title: string }) => <h1>{title}</h1> }));
vi.mock("@/lib/api/operation-history", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/api/operation-history")>(),
  getOperationHistory: state.get,
}));

function response(target = "visible-target", next: string | null = null): HistoryPage {
  return { total: 1, next_cursor: next, items: [{
    id: target, occurred_at: "2026-11-09T00:00:00Z", context_id: "context-id",
    actor_user_id: "actor-id", actor_name: "Alice", subject_user_id: null,
    agent_id: "verified-agent", surface: "mcp", operation: "memory.updated",
    target_type: "memory", target_id: target, outcome: "success", count: 1, changed_fields: ["tags"],
  }] };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe("workspace operation history", () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.gate = "allowed"; state.errorGate = false;
    state.workspace = { id: "workspace-one", current_user_role: "owner", plan_name: "promax" };
    state.get.mockResolvedValue(response());
  });

  it("renders actor, verified agent, change and authorized detail link", async () => {
    render(<Page />);
    expect(await screen.findByText("visible-target")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("agent:verified-agent")).toBeInTheDocument();
    expect(screen.getByText("fields.tags")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "details" })).toHaveAttribute("href", "/workspace/contexts/context-id?tab=memories&memoryId=visible-target");
  });

  it.each(["plan", "role", "pending"])("does not request history while gate is %s", async gate => {
    state.gate = gate;
    render(<Page />);
    expect(state.get).not.toHaveBeenCalled();
    if (gate !== "pending") expect(screen.getByText(`gate:${gate}`)).toBeInTheDocument();
  });

  it("applies combined search and identity filters, clearing the cursor", async () => {
    state.get.mockResolvedValue(response("first", "next-cursor"));
    render(<HistoryView workspaceId="workspace-one" />);
    await screen.findByText("first");
    fireEvent.click(screen.getByRole("button", { name: "next" }));
    await waitFor(() => expect(state.get.mock.lastCall?.[1].get("cursor")).toBe("next-cursor"));
    fireEvent.change(screen.getByLabelText("query"), { target: { value: "Alice %" } });
    fireEvent.change(screen.getByLabelText("actor"), { target: { value: "actor-id" } });
    fireEvent.change(screen.getByLabelText("context_id"), { target: { value: "11111111-1111-4111-8111-111111111111" } });
    fireEvent.submit(screen.getByRole("form", { name: "filters" }));
    await waitFor(() => {
      const params = state.get.mock.lastCall?.[1] as URLSearchParams;
      expect(params.get("query")).toBe("Alice %");
      expect(params.get("actor")).toBe("actor-id");
      expect(params.get("context_id")).toBe("11111111-1111-4111-8111-111111111111");
      expect(params.has("cursor")).toBe(false);
    });
  });

  it("discards a late response after workspace switching and aborts its request", async () => {
    const old = deferred<HistoryPage>();
    state.get.mockReturnValueOnce(old.promise).mockResolvedValueOnce(response("new-workspace"));
    const view = render(<HistoryView workspaceId="workspace-one" />);
    const signal = state.get.mock.calls[0][2] as AbortSignal;
    view.rerender(<HistoryView workspaceId="workspace-two" />);
    expect(signal.aborted).toBe(true);
    await screen.findByText("new-workspace");
    await act(async () => { old.resolve(response("old-private-data")); });
    expect(screen.queryByText("old-private-data")).not.toBeInTheDocument();
  });

  it("removes loaded history immediately after local role revocation", async () => {
    const view = render(<Page />);
    await screen.findByText("visible-target");
    state.gate = "role"; state.workspace.current_user_role = "member";
    view.rerender(<Page />);
    expect(screen.queryByText("visible-target")).not.toBeInTheDocument();
    expect(screen.getByText("gate:role")).toBeInTheDocument();
  });

  it("clears rows and presents upgrade guidance when a refreshed read is denied", async () => {
    render(<Page />);
    await screen.findByText("visible-target");
    state.errorGate = true; state.get.mockRejectedValueOnce(new Error("plan denied"));
    fireEvent.click(screen.getByRole("button", { name: "refresh" }));
    expect(screen.queryByText("visible-target")).not.toBeInTheDocument();
    expect(await screen.findByText("gate:plan")).toBeInTheDocument();
  });

  it("shows empty results separately from load errors", async () => {
    state.get.mockResolvedValueOnce({ items: [], total: 0, next_cursor: null });
    render(<HistoryView workspaceId="workspace-one" />);
    expect(await screen.findByText("empty")).toBeInTheDocument();
    state.get.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByRole("button", { name: "refresh" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("error");
    expect(screen.queryByText("empty")).not.toBeInTheDocument();
  });

  it("rechecks authorization on focus and cancels in-flight work on unmount", async () => {
    const view = render(<HistoryView workspaceId="workspace-one" />);
    await screen.findByText("visible-target");
    fireEvent(window, new Event("focus"));
    await waitFor(() => expect(state.get).toHaveBeenCalledTimes(2));
    const signal = state.get.mock.lastCall?.[2] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
  });
});
