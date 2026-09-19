/**
 * SettingsTabPanel — sharing gate and privacy payload (#1583).
 *
 * A sleep-mode edit on a plan without `shared_contexts` used to be refused by
 * the sharing gate because the form sent `is_private` although the user never
 * touched it. Pins:
 *   - the payload holds only touched fields whatever the stored `is_private`
 *     is (`undefined` / `true` / `false`);
 *   - without the plan feature "Make shared" is inert on a private context
 *     (the upgrade notice explains why), while a legacy shared context can
 *     still be made private;
 *   - a FEAT-001 refusal names the control, not the raw feature key.
 *
 * Select is rendered natively (same idiom as the admin plans page test) so
 * the sleep-mode control can be driven — Radix Select does not respond to
 * fireEvent in happy-dom.
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsTabPanel } from "./SettingsTabPanel";
import { ApiError } from "@/lib/api/base";
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

// Key-as-text translator; `plan` is folded in so the label is assertable.
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) =>
      vars && "plan" in vars ? `${key}:${vars.plan}` : key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: {
      id: "ws-1",
      current_user_role: "owner",
      plan_name: "basic",
    },
    currentWorkspaceId: "ws-1",
  }),
}));

// Tri-state per feature (`null` = the tier matrix is still resolving).
let mockPlanFeatures: Record<string, boolean | null> = {};
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanFeature: (feature: string) => mockPlanFeatures[feature] ?? null,
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1" } }),
}));

type SelectChildren = { children: React.ReactNode };
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children: React.ReactNode;
  }) => (
    <select
      data-testid={value === undefined ? "template-select" : "sleep-select"}
      value={value ?? ""}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: SelectChildren) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: SelectChildren) => <>{children}</>,
  SelectItem: ({ value }: { value: string }) => (
    <option value={value}>{value}</option>
  ),
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
    sleep_mode: "skip",
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

/** A context as an older API serialises it: the privacy flags are absent. */
function makeLegacyContext(): Context {
  const legacy: Partial<Context> = makeContext();
  delete legacy.is_private;
  delete legacy.is_public;
  return legacy as Context;
}

function renderPanel(context: Context) {
  mockGetContext.mockResolvedValue(context);
  render(
    <SettingsTabPanel
      contextId={CTX_ID}
      context={context}
      onContextUpdated={() => {}}
    />,
  );
}

async function changeSleepModeAndSave(mode: "full" | "edges_only") {
  fireEvent.change(screen.getByTestId("sleep-select"), {
    target: { value: mode },
  });
  fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));
  await waitFor(() => expect(mockUpdateContext).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUpdateContext.mockResolvedValue(undefined);
  mockGetWorkspaceUsageCurrent.mockResolvedValue({
    usage: {
      sleep_contexts: { used: 0, limit: 3, addon_bonus: 0, remaining: 3 },
    },
  });
  // M plan: neither sharing nor public access.
  mockPlanFeatures = { shared_contexts: false, public_contexts: false };
});

// ---------- Payload holds only touched fields --------------------------------

describe("SettingsTabPanel — untouched privacy never enters the payload (#1583)", () => {
  it.each([
    ["undefined (legacy response)", makeLegacyContext()],
    ["true", makeContext({ is_private: true })],
    ["false (legacy shared context)", makeContext({ is_private: false })],
  ])(
    "stored is_private = %s: a sleep-mode edit sends sleep_mode alone",
    async (_label, context) => {
      renderPanel(context);

      await changeSleepModeAndSave("full");

      expect(mockUpdateContext).toHaveBeenCalledWith(CTX_ID, {
        sleep_mode: "full",
      });
    },
  );
});

// ---------- Sharing control without the plan feature -------------------------

describe("SettingsTabPanel — sharing control without shared_contexts (#1583)", () => {
  it("private context: Make shared is inert and the upgrade notice explains why", () => {
    renderPanel(makeContext({ is_private: true }));

    const makeShared = screen.getByRole("button", { name: "makeShared" });
    expect(makeShared).toBeDisabled();
    const notice = screen.getByText("sharedRequiresPlan:L");
    expect(makeShared.getAttribute("aria-describedby")).toBe(
      notice.closest("[id]")?.id,
    );
    // The "make it Shared first" hint would point at the disabled button.
    expect(screen.queryByText("makeSharedFirst")).toBeNull();

    fireEvent.click(makeShared);
    expect(screen.queryByRole("button", { name: /saveChanges/ })).toBeNull();
    expect(screen.getByRole("button", { name: "makeShared" })).toBeTruthy();
  });

  it("private context: a sleep-mode edit cannot be bundled with is_private", async () => {
    renderPanel(makeContext({ is_private: true }));

    fireEvent.click(screen.getByRole("button", { name: "makeShared" }));
    await changeSleepModeAndSave("edges_only");

    expect(mockUpdateContext).toHaveBeenCalledWith(CTX_ID, {
      sleep_mode: "edges_only",
    });
  });

  it("pending matrix: the control waits, without an upsell", () => {
    mockPlanFeatures = { shared_contexts: null, public_contexts: null };
    renderPanel(makeContext({ is_private: true }));

    expect(screen.getByRole("button", { name: "makeShared" })).toBeDisabled();
    expect(screen.queryByText(/sharedRequiresPlan/)).toBeNull();
    expect(screen.getByText("makeSharedFirst")).toBeInTheDocument();
  });

  it("plan includes sharing: Make shared works and is sent", async () => {
    mockPlanFeatures = { shared_contexts: true, public_contexts: false };
    renderPanel(makeContext({ is_private: true }));

    expect(screen.queryByText(/sharedRequiresPlan/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "makeShared" }));
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));

    await waitFor(() => expect(mockUpdateContext).toHaveBeenCalledTimes(1));
    expect(mockUpdateContext).toHaveBeenCalledWith(CTX_ID, {
      is_private: false,
    });
  });

  // The dialog's action button carries the same label as the card's toggle.
  async function makePrivateAndConfirm() {
    fireEvent.click(screen.getByRole("button", { name: "makePrivate" }));
    await screen.findByText("makePrivateTitle");
    fireEvent.click(
      screen
        .getAllByRole("button", { name: "makePrivate" })
        .at(-1) as HTMLElement,
    );
  }

  it("legacy shared context: can still be made private", async () => {
    renderPanel(makeContext({ is_private: false }));

    expect(screen.queryByText(/sharedRequiresPlan/)).toBeNull();
    await makePrivateAndConfirm();
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));

    await waitFor(() => expect(mockUpdateContext).toHaveBeenCalledTimes(1));
    expect(mockUpdateContext).toHaveBeenCalledWith(CTX_ID, {
      is_private: true,
    });
  });

  it("legacy shared context: undoing a pending Make private is not locked", async () => {
    // Back to the stored value is not a transition, so the plan has no say.
    renderPanel(makeContext({ is_private: false }));

    await makePrivateAndConfirm();
    const undo = await screen.findByRole("button", { name: "makeShared" });
    expect(undo).not.toBeDisabled();
    expect(screen.queryByText(/sharedRequiresPlan/)).toBeNull();

    fireEvent.click(undo);
    expect(
      await screen.findByRole("button", { name: "makePrivate" }),
    ).toBeInTheDocument();
  });
});

// ---------- FEAT-001 toast names the control ---------------------------------

describe("SettingsTabPanel — FEAT-001 refusal names the control (#1583)", () => {
  function refuse(feature: string) {
    mockUpdateContext.mockRejectedValue(
      new ApiError({
        error: "FEAT-001",
        message: `Feature '${feature}' not available on M plan.`,
        status: 403,
        details: { feature },
      }),
    );
  }

  async function saveARename() {
    fireEvent.change(screen.getByDisplayValue("Demo Context"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    return mockToast.mock.calls[0][0] as { title: string; description: string };
  }

  it.each([
    ["shared_contexts", "sharedRequiresPlan:L"],
    ["public_contexts", "publicRequiresPlan:XL"],
  ])("%s → %s", async (feature, expected) => {
    refuse(feature);
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.title).toBe("saveFailedTitle");
    expect(toast.description).toBe(expected);
  });

  it("an unknown feature falls back to the server text", async () => {
    refuse("connectors");
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.description).toBe(
      "Feature 'connectors' not available on M plan.",
    );
  });
});
