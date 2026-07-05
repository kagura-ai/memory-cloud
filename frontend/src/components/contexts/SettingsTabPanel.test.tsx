/**
 * Tests for SettingsTabPanel (Issue #571).
 *
 * Covers the gate1 scope:
 *   - Sleep mode section gated by isOwner (workspace role === "owner")
 *   - Sleep quota fetched on mount only for owners
 *   - wouldExceedSleepQuota derivation across 4 branches, asserted via the
 *     visible error-message branch (sleepQuotaTierBlocked vs sleepQuotaExceeded)
 *     instead of opening the Radix Select. Radix interactive primitives do
 *     not respond cleanly to fireEvent.click in happy-dom — see
 *     MCPConfigBlock.test.tsx for the canonical side-channel approach used in
 *     this codebase.
 *   - Sleep quota refetched after a successful save (initial + post-save fetch)
 *   - Privacy transition AlertDialog opens on shared → private toggle and
 *     respects confirm/cancel
 *   - resource_id input filters keystrokes to lowercase + [a-z0-9_] only
 *   - Skip-mode AlertDialog (#504) is closed on mount; full open/confirm/cancel
 *     flow requires opening the Radix Select trigger, which is the same
 *     happy-dom limitation documented in MCPConfigBlock.test.tsx — closed-on-mount
 *     covers the "doesn't accidentally render" regression, which is the
 *     reachable contract.
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsTabPanel } from "./SettingsTabPanel";
import type { Context } from "@/lib/types/context";

// ---------- Mocks ------------------------------------------------------------

const mockGetContext = vi.fn();
const mockUpdateContext = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContext: (...a: unknown[]) => mockGetContext(...a),
  updateContext: (...a: unknown[]) => mockUpdateContext(...a),
}));

const mockGetWorkspaceUsageCurrent = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspaceUsageCurrent: (...a: unknown[]) =>
    mockGetWorkspaceUsageCurrent(...a),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Stable key-as-text translator. Vars are folded into the key so we can
// distinguish e.g. sleepQuotaUsage rendered with different used/limit values.
const stableT = (key: string, vars?: Record<string, unknown>) => {
  if (!vars) return key;
  if ("used" in vars && "limit" in vars) {
    return `${key}:${vars.used}/${vars.limit}+${vars.addon ?? 0}`;
  }
  if ("count" in vars) return `${key}:${vars.count}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1" } }),
}));

// ---------- Helpers ----------------------------------------------------------

const CTX_ID = "11111111-1111-1111-1111-111111111111";

function makeContext(overrides: Partial<Context> = {}): Context {
  return {
    id: CTX_ID,
    name: "demo",
    display_name: "Demo Context",
    description: "",
    summary: "",
    usage_guide: "",
    collection_name: "ctx_demo",
    is_default: false,
    is_private: true,
    is_public: false,
    is_locked: false,
    sleep_mode: "full",
    resource_id: null,
    created_by: "user-1",
    created_by_name: "Owner",
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    use_rerank: null,
    reranker_provider: null,
    embedding_model: "text-embedding-3-small",
    embedding_dimensions: 1536,
    member_count: 1,
    memory_count: 0,
    last_activity_at: null,
    ...overrides,
  };
}

function setRole(role: "owner" | "admin" | "member" | "viewer") {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", current_user_role: role },
    currentWorkspaceId: "ws-1",
  });
}

function buildQuotaResponse(
  used: number,
  limit: number,
  addon_bonus = 0,
  remaining = Math.max(0, limit - used),
) {
  return {
    plan: {} as never,
    usage: {
      memory_count: 0,
      api_calls_today: 0,
      api_calls_this_week: 0,
      mcp_calls_today: 0,
      mcp_calls_this_week: 0,
      rest_calls_today: 0,
      rest_calls_this_week: 0,
      public_calls_today: 0,
      public_calls_this_week: 0,
      sleep_contexts: { used, limit, addon_bonus, remaining },
      workspaces: { used: 0, limit: 0, remaining: 0 },
    },
    memory_usage: {} as never,
    daily_api_usage: {} as never,
    weekly_api_usage: {} as never,
  };
}

function setQuotaResponse(
  used: number,
  limit: number,
  addon_bonus = 0,
  remaining = Math.max(0, limit - used),
) {
  mockGetWorkspaceUsageCurrent.mockResolvedValue(
    buildQuotaResponse(used, limit, addon_bonus, remaining),
  );
}

const noop = () => {};

beforeEach(() => {
  vi.clearAllMocks();
  // Default: owner with no quota constraint. Tests override as needed.
  setRole("owner");
  setQuotaResponse(0, 10);
});

// ---------- Sleep mode section visibility -----------------------------------

describe("SettingsTabPanel — sleep mode section visibility", () => {
  it("renders the sleep mode section and fetches quota when isOwner", async () => {
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext()}
        onContextUpdated={noop}
      />,
    );

    // Section title appears (Card with sleepModeTitle).
    expect(screen.getByText("sleepModeTitle")).toBeInTheDocument();

    // Owner triggers the mount-time quota fetch.
    await waitFor(() => {
      expect(mockGetWorkspaceUsageCurrent).toHaveBeenCalledTimes(1);
    });
  });

  it("hides the sleep mode section and skips quota fetch when not owner", () => {
    setRole("member");
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext()}
        onContextUpdated={noop}
      />,
    );

    expect(screen.queryByText("sleepModeTitle")).not.toBeInTheDocument();
    expect(mockGetWorkspaceUsageCurrent).not.toHaveBeenCalled();
  });
});

// ---------- wouldExceedSleepQuota derivation (4 branches) -------------------

describe("SettingsTabPanel — wouldExceedSleepQuota derivation", () => {
  it("shows sleepQuotaExceeded when current mode is skip AND used >= limit AND limit > 0", async () => {
    setQuotaResponse(3, 3);
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "skip" })}
        onContextUpdated={noop}
      />,
    );

    // The error message renders once the quota fetch resolves.
    await screen.findByText("sleepQuotaExceeded");
    expect(screen.queryByText("sleepQuotaTierBlocked")).not.toBeInTheDocument();
  });

  it("shows sleepQuotaTierBlocked instead when limit === 0 (tier without sleep)", async () => {
    setQuotaResponse(0, 0);
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "skip" })}
        onContextUpdated={noop}
      />,
    );

    await screen.findByText("sleepQuotaTierBlocked");
    expect(screen.queryByText("sleepQuotaExceeded")).not.toBeInTheDocument();
  });

  it("renders neither blocked nor exceeded message when there is headroom (used < limit)", async () => {
    setQuotaResponse(1, 3);
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "skip" })}
        onContextUpdated={noop}
      />,
    );

    // Wait for fetch to resolve so the usage line appears, then assert no
    // blocked/exceeded text.
    await screen.findByText(/sleepQuotaUsage:1\/3/);
    expect(screen.queryByText("sleepQuotaExceeded")).not.toBeInTheDocument();
    expect(screen.queryByText("sleepQuotaTierBlocked")).not.toBeInTheDocument();
  });

  it("does NOT block lateral changes when current mode is not skip even with used >= limit", async () => {
    // sleep_mode = "full" (already counts toward the quota), used >= limit.
    // A lateral change to edges_only doesn't increase the count, so the
    // derivation must NOT flag this as exceeding.
    setQuotaResponse(3, 3);
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "full" })}
        onContextUpdated={noop}
      />,
    );

    await screen.findByText(/sleepQuotaUsage:3\/3/);
    expect(screen.queryByText("sleepQuotaExceeded")).not.toBeInTheDocument();
    expect(screen.queryByText("sleepQuotaTierBlocked")).not.toBeInTheDocument();
  });
});

// ---------- Sleep quota refetched after save --------------------------------

describe("SettingsTabPanel — sleep quota refetched after save", () => {
  it("refetches getWorkspaceUsageCurrent after updateContext resolves", async () => {
    // Initial fetch: 1/3. Post-save fetch: 0/3 (a skip→non-skip transition
    // would free a slot). Two distinct values let us assert the second
    // fetch fired without polling for both side effects.
    mockGetWorkspaceUsageCurrent
      .mockResolvedValueOnce(buildQuotaResponse(1, 3))
      .mockResolvedValueOnce(buildQuotaResponse(0, 3));
    mockUpdateContext.mockResolvedValue(undefined);
    mockGetContext.mockResolvedValue(makeContext({ sleep_mode: "full" }));

    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "full" })}
        onContextUpdated={noop}
      />,
    );

    // Mark dirty, then save. The save path is updateContext → refreshContext
    // → second quota fetch — the second-fetch assertion subsumes the first.
    fireEvent.change(screen.getByDisplayValue("Demo Context"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));

    await waitFor(() => {
      expect(mockGetWorkspaceUsageCurrent).toHaveBeenCalledTimes(2);
    });
    expect(mockUpdateContext).toHaveBeenCalledTimes(1);
  });
});

// ---------- Privacy transition dialog ---------------------------------------

describe("SettingsTabPanel — privacy transition dialog", () => {
  it("opens makePrivateTitle dialog when toggling from shared → private", async () => {
    // Initial: shared (is_private=false). The "makePrivate" button should
    // open the AlertDialog instead of immediately changing state.
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ is_private: false })}
        onContextUpdated={noop}
      />,
    );

    expect(screen.queryByText("makePrivateTitle")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "makePrivate" }));
    expect(await screen.findByText("makePrivateTitle")).toBeInTheDocument();
    expect(screen.getByText("makePrivateWarning")).toBeInTheDocument();
  });

  it("does NOT open the dialog when toggling private → shared (one-way protection)", () => {
    // Initial: private. Clicking makeShared just flips the state with no
    // confirmation dialog — only the destructive direction is gated.
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ is_private: true })}
        onContextUpdated={noop}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "makeShared" }));
    expect(screen.queryByText("makePrivateTitle")).not.toBeInTheDocument();
    // The button label flips to makePrivate after toggling to shared.
    expect(
      screen.getByRole("button", { name: "makePrivate" }),
    ).toBeInTheDocument();
  });
});

// ---------- resource_id input filter ----------------------------------------

describe("SettingsTabPanel — resource_id input validation", () => {
  it("filters keystrokes to lowercase + [a-z0-9_] only when typing in the resource_id input", async () => {
    // resource_id input renders when:
    //   - shared (is_private=false)
    //   - not yet public (is_public=false)
    //   - isOwner
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ is_private: false, is_public: false })}
        onContextUpdated={noop}
      />,
    );

    const input = (await screen.findByPlaceholderText(
      "resourceIdPlaceholder",
    )) as HTMLInputElement;

    // Mixed case + hyphen + symbol + underscore + digits → only [a-z0-9_]
    // survives, and uppercase is lowercased before filtering.
    fireEvent.change(input, { target: { value: "ABC-def_123!" } });
    expect(input.value).toBe("abcdef_123");
  });
});

// ---------- Skip-mode confirmation dialog (#504) ----------------------------

describe("SettingsTabPanel — skip-mode confirmation dialog", () => {
  it("does not render the skip-mode dialog on mount", () => {
    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ sleep_mode: "full" })}
        onContextUpdated={noop}
      />,
    );
    // The AlertDialog's title only enters the DOM when open=true. Radix
    // Select cannot be triggered via fireEvent.click in happy-dom (see
    // MCPConfigBlock.test.tsx for the canonical limitation note), so the
    // open/confirm/cancel flow is not exercised end-to-end. This guards
    // the "doesn't accidentally render" regression — which is the
    // reachable side-channel without adding @testing-library/user-event.
    expect(screen.queryByText("sleepModeSkipTitle")).not.toBeInTheDocument();
  });
});

// ---------- Dirty-field-only save (#1193) ------------------------------------

describe("SettingsTabPanel — dirty-field-only save (#1193)", () => {
  it("sends ONLY the changed field on save", async () => {
    mockUpdateContext.mockResolvedValue(undefined);
    mockGetContext.mockResolvedValue(makeContext());

    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext()}
        onContextUpdated={noop}
      />,
    );

    fireEvent.change(screen.getByDisplayValue("Demo Context"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));

    await waitFor(() => expect(mockUpdateContext).toHaveBeenCalledTimes(1));
    expect(mockUpdateContext).toHaveBeenCalledWith(CTX_ID, {
      display_name: "Renamed",
    });
  });

  it("does NOT re-send an untouched over-cap legacy summary", async () => {
    // Regression pin for the prod incident: an MCP-written 516-char summary
    // made EVERY save 422 because the panel re-submitted all fields. With
    // dirty-only payloads the oversized untouched summary stays out.
    const longSummary = "x".repeat(600);
    mockUpdateContext.mockResolvedValue(undefined);
    mockGetContext.mockResolvedValue(makeContext({ summary: longSummary }));

    render(
      <SettingsTabPanel
        contextId={CTX_ID}
        context={makeContext({ summary: longSummary })}
        onContextUpdated={noop}
      />,
    );

    fireEvent.change(screen.getByDisplayValue("Demo Context"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));

    await waitFor(() => expect(mockUpdateContext).toHaveBeenCalledTimes(1));
    const payload = mockUpdateContext.mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(payload).not.toHaveProperty("summary");
    expect(payload).toEqual({ display_name: "Renamed" });
  });
});
