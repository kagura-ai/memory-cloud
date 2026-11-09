import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Page from "./page";
import { HistoryView } from "./HistoryView";
import type { HistoryPage } from "@/lib/api/operation-history";
import { ApiError } from "@/lib/api/base";

const state = vi.hoisted(() => ({
  get: vi.fn(), gate: "allowed", errorGate: false, timezone: "UTC",
  workspace: { id: "workspace-one", current_user_role: "owner", plan_name: "promax" },
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => values ? `${key}:${Object.values(values).join(",")}` : key,
}));
vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({ currentWorkspace: state.workspace }) }));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ user: { timezone: state.timezone } }) }));
vi.mock("@/hooks/useFeatureGate", () => ({ useFeatureGate: () => ({ state: state.gate }) }));
vi.mock("@/hooks/useErrorGate", () => ({ useErrorGate: (error: unknown) => error && state.errorGate ? { state: "plan" } : null }));
vi.mock("@/components/common/FeatureGateNotice", () => ({ FeatureGateNotice: ({ gate }: { gate: { state: string } }) => <p>gate:{gate.state}</p> }));
vi.mock("@/components/dashboard/WorkspacePageHeader", () => ({ WorkspacePageHeader: ({ title }: { title: string }) => <h1>{title}</h1> }));
vi.mock("@/lib/api/operation-history", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/api/operation-history")>(),
  getOperationHistory: state.get,
}));

function response(target = "visible-target", next: string | null = null): HistoryPage {
  return { total: 1, total_capped: false, next_cursor: next, items: [{
    id: target, occurred_at: "2026-11-09T00:00:00Z", context_id: "context-id",
    actor_user_id: "actor-id", actor_name: "Alice", subject_user_id: null,
    principal_kind: "session", credential_id: null, client_id: null,
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
    vi.clearAllMocks(); state.gate = "allowed"; state.errorGate = false; state.timezone = "UTC";
    state.workspace = { id: "workspace-one", current_user_role: "owner", plan_name: "promax" };
    state.get.mockResolvedValue(response());
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
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
    expect(await screen.findByText("gate:plan")).toBeInTheDocument();
    expect(screen.queryByText("visible-target")).not.toBeInTheDocument();
  });

  it("shows empty results separately from load errors", async () => {
    state.get.mockResolvedValueOnce({ items: [], total: 0, total_capped: false, next_cursor: null });
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

  it.each(["poll", "focus"])("keeps the table and focused link during %s revalidation", async source => {
    vi.useFakeTimers();
    render(<HistoryView workspaceId="workspace-one" />);
    await act(async () => {});
    const link = screen.getByRole("link", { name: "details" });
    const refresh = deferred<HistoryPage>();
    state.get.mockReturnValueOnce(refresh.promise);
    link.focus();
    if (source === "poll") {
      await act(async () => { vi.advanceTimersByTime(30_000); });
    } else {
      fireEvent(window, new Event("focus"));
    }
    expect(state.get).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("link", { name: "details" })).toBe(link);
    expect(link).toHaveFocus();
    await act(async () => { refresh.resolve(response()); });
    expect(screen.getByRole("link", { name: "details" })).toBe(link);
    expect(link).toHaveFocus();
  });

  it("does not poll hidden tabs and revalidates when the tab becomes visible", async () => {
    vi.useFakeTimers();
    render(<HistoryView workspaceId="workspace-one" />);
    await act(async () => {});
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => {
      vi.advanceTimersByTime(60_000);
      fireEvent(window, new Event("focus"));
    });
    expect(state.get).toHaveBeenCalledTimes(1);
    visibility.mockReturnValue("visible");
    await act(async () => { fireEvent(document, new Event("visibilitychange")); });
    expect(state.get).toHaveBeenCalledTimes(2);
  });

  it.each([[403, "forbidden"], [401, "sessionExpired"]])("clears rows after HTTP %s and explains lost access", async (status, message) => {
    render(<HistoryView workspaceId="workspace-one" />);
    await screen.findByText("visible-target");
    state.get.mockRejectedValueOnce(new ApiError({ status: Number(status), message: "refused" }));
    fireEvent(window, new Event("focus"));
    expect(await screen.findByRole("alert")).toHaveTextContent(String(message));
    expect(screen.queryByText("visible-target")).not.toBeInTheDocument();
  });

  it.each(["api_key", "oauth", "resource_token", "management_grant"] as const)("shows the %s credential separately from its account and the affected member", async kind => {
    const data = response("target-key");
    Object.assign(data.items[0], {
      principal_kind: kind, credential_id: "acting-key", client_id: "known-client",
      subject_user_id: "affected-member", target_type: "credential", operation: "credential.revoked",
    });
    state.get.mockResolvedValueOnce(data);
    render(<HistoryView workspaceId="workspace-one" />);
    expect(await screen.findByText("credential:acting-key")).toBeInTheDocument();
    expect(screen.getByText("client:known-client")).toBeInTheDocument();
    expect(screen.getByText("principalAccount:Alice (actor-id)")).toBeInTheDocument();
    expect(screen.getByText("subject:affected-member")).toBeInTheDocument();
    expect(screen.getByText("target-key")).toBeInTheDocument();
    expect(screen.queryByText("Alice", { exact: true })).not.toBeInTheDocument();
  });

  it("does not turn an unknown principal into a known person", async () => {
    const data = response(); data.items[0].principal_kind = "unknown";
    state.get.mockResolvedValueOnce(data);
    render(<HistoryView workspaceId="workspace-one" />);
    expect(await screen.findByText("unknown")).toBeInTheDocument();
    expect(screen.queryByText("Alice", { exact: true })).not.toBeInTheDocument();
  });

  it("uses the saved timezone for display and includes the whole ending minute", async () => {
    state.timezone = "Asia/Tokyo";
    render(<HistoryView workspaceId="workspace-one" />);
    await screen.findByText("visible-target");
    expect(screen.getByText("11/09/2026, 09:00:00 AM")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("since:Asia/Tokyo"), { target: { value: "2026-11-09T09:00" } });
    fireEvent.change(screen.getByLabelText("until:Asia/Tokyo"), { target: { value: "2026-11-09T09:00" } });
    fireEvent.submit(screen.getByRole("form", { name: "filters" }));
    await waitFor(() => {
      const params = state.get.mock.lastCall?.[1] as URLSearchParams;
      expect(params.get("since")).toBe("2026-11-09T00:00:00.000Z");
      expect(params.get("until")).toBe("2026-11-09T00:00:59.999999Z");
    });
  });

  it("labels capped counts and uses the configured plan name", async () => {
    vi.stubEnv("NEXT_PUBLIC_PLAN_PROMAX_DISPLAY_NAME", "Team Plus");
    state.get.mockResolvedValueOnce({ ...response(), total: 1000, total_capped: true });
    render(<HistoryView workspaceId="workspace-one" />);
    expect(await screen.findByText("totalCapped:1000")).toBeInTheDocument();
    expect(screen.getByText("coverage:Team Plus")).toBeInTheDocument();
  });
});
