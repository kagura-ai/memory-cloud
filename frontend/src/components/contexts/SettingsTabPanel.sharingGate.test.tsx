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
 *   - a FEAT-001 refusal names the control, not the raw feature key, and
 *     (#1644) the tier the refusal itself names, not a hardcoded one.
 *
 * #1646: the notice is FeatureGateNotice (inline, scope "create") and the
 * refusal toast is featureGateToast — both read `gate.*`, so under this
 * key-as-text translator they render e.g. "plan.newTitle:L".
 *
 * Select is rendered natively (same idiom as the admin plans page test) so
 * the sleep-mode control can be driven — Radix Select does not respond to
 * fireEvent in happy-dom.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsTabPanel } from "./SettingsTabPanel";
import { ApiError } from "@/lib/api/base";
import { normalizeGate } from "@/lib/gates/featureGates";
import type { Context } from "@/lib/types/context";
import type { PlanTierFeature } from "@/lib/api/workspaces";

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

// #1646: FeatureGateNotice's upgrade CTA navigates with the app router.
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
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
// #1645: read through useFeatureGates; each tri-state maps onto a descriptor
// naming the tier the default matrix gives the feature.
let mockPlanFeatures: Record<string, boolean | null> = {};
const REQUIRED: Record<string, [string, string]> = {
  shared_contexts: ["pro", "L"],
  public_contexts: ["promax", "XL"],
  sleep_reports: ["pro", "L"],
};
// A test that moves a feature to another tier (or off every tier: `null`)
// names it here; the matrix, not this form, decides the tier.
let mockRequired: Record<string, [string, string] | null> = {};
// #1646: what the hook's `canUpgrade` says for a plan gate that names a tier.
let mockCanUpgrade = false;
const gateCache = new Map<string, unknown>();
function gateFor(feature: string) {
  const value = mockPlanFeatures[feature] ?? null;
  const required =
    feature in mockRequired ? mockRequired[feature] : REQUIRED[feature];
  const cacheKey = `${feature}:${value}:${required}:${mockCanUpgrade}`;
  if (!gateCache.has(cacheKey)) {
    const [requiredPlan, planLabel] = required ?? [undefined, undefined];
    gateCache.set(
      cacheKey,
      value === null
        ? { state: "pending", feature, canUpgrade: false }
        : value
          ? { state: "allowed", feature, canUpgrade: false }
          : {
              state: "plan",
              feature,
              requiredPlan,
              planLabel,
              canUpgrade: mockCanUpgrade && required !== null,
            },
    );
  }
  return gateCache.get(cacheKey);
}
vi.mock("@/hooks/useFeatureGate", () => ({
  useFeatureGates: (keys: string[]) =>
    Object.fromEntries(keys.map((key) => [key, gateFor(key)])),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-1" } }),
}));

// #1645: save refusals are lifted with the shared tier matrix. `null` (still
// resolving) by default, so a refusal is read from its own facts alone.
let mockTiers: PlanTierFeature[] | null = null;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
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
  mockTiers = null;
  mockUpdateContext.mockResolvedValue(undefined);
  mockGetWorkspaceUsageCurrent.mockResolvedValue({
    usage: {
      sleep_contexts: { used: 0, limit: 3, addon_bonus: 0, remaining: 3 },
    },
  });
  // M plan: neither sharing nor public access.
  mockPlanFeatures = { shared_contexts: false, public_contexts: false };
  mockRequired = {};
  mockCanUpgrade = false;
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
    const notice = screen.getByText("plan.newTitle:L");
    expect(screen.getByText("plan.newDescription:L")).toBeInTheDocument();
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

  it("the notice names the tier the matrix names, not a hardcoded one (#1645)", () => {
    // An operator moved shared_contexts down to basic (M).
    mockRequired = { shared_contexts: ["basic", "M"] };
    renderPanel(makeContext({ is_private: true }));

    expect(screen.getByText("plan.newTitle:M")).toBeInTheDocument();
    expect(screen.queryByText("plan.newTitle:L")).toBeNull();
  });

  it("no served tier has sharing: the control stays inert, with no tier to name (#1645)", () => {
    mockRequired = { shared_contexts: null };
    mockCanUpgrade = true;
    renderPanel(makeContext({ is_private: true }));

    const makeShared = screen.getByRole("button", { name: "makeShared" });
    expect(makeShared).toBeDisabled();
    // #1646: the tier-less copy explains it; no tier is named, nothing is
    // offered, and the control points at the notice that is there.
    const notice = screen.getByText("plan.titleNoTier");
    expect(screen.getByText("plan.descriptionNoTier")).toBeInTheDocument();
    expect(screen.queryByText(/^plan\.newTitle/)).toBeNull();
    expect(screen.queryByRole("button", { name: /plan\.action/ })).toBeNull();
    expect(makeShared.getAttribute("aria-describedby")).toBe(
      notice.closest("[id]")?.id,
    );
    expect(screen.queryByText("makeSharedFirst")).toBeNull();
  });

  it("the sharing notice keeps the id the control points at (#1646)", () => {
    renderPanel(makeContext({ is_private: true }));

    const makeShared = screen.getByRole("button", { name: "makeShared" });
    const id = makeShared.getAttribute("aria-describedby");
    expect(id).toBe("context-sharing-upgrade-notice");
    expect(document.getElementById(id as string)).toContainElement(
      screen.getByText("plan.newTitle:L"),
    );
  });

  it("the sharing notice offers the upgrade only where the gate allows it (#1646)", () => {
    renderPanel(makeContext({ is_private: true }));
    expect(screen.queryByRole("button", { name: /plan\.action/ })).toBeNull();
    cleanup();

    mockCanUpgrade = true;
    renderPanel(makeContext({ is_private: true }));
    fireEvent.click(screen.getByRole("button", { name: "plan.action:L" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("pending matrix: the control waits, without an upsell", () => {
    mockPlanFeatures = { shared_contexts: null, public_contexts: null };
    renderPanel(makeContext({ is_private: true }));

    expect(screen.getByRole("button", { name: "makeShared" })).toBeDisabled();
    expect(screen.queryByText(/^plan\./)).toBeNull();
    expect(screen.getByText("makeSharedFirst")).toBeInTheDocument();
  });

  it("plan includes sharing: Make shared works and is sent", async () => {
    mockPlanFeatures = { shared_contexts: true, public_contexts: false };
    renderPanel(makeContext({ is_private: true }));

    expect(screen.queryByText(/^plan\./)).toBeNull();
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

    // The sharing notice (L); the public one (XL) is not about this control.
    expect(screen.queryByText("plan.newTitle:L")).toBeNull();
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
    expect(screen.queryByText("plan.newTitle:L")).toBeNull();

    fireEvent.click(undo);
    expect(
      await screen.findByRole("button", { name: "makePrivate" }),
    ).toBeInTheDocument();
  });
});

// ---------- FEAT-001 toast names the control ---------------------------------

describe("SettingsTabPanel — FEAT-001 refusal names the control (#1583)", () => {
  /** A FEAT-001 exactly as lib/api/base.ts builds it from a #1644 body. */
  function refuse(feature: string, requiredPlan: string | null = null) {
    const details = {
      gate: "plan",
      feature,
      required_plan: requiredPlan,
      required_plan_display: null,
      current_plan: "basic",
    };
    mockUpdateContext.mockRejectedValue(
      new ApiError({
        error: "FEAT-001",
        message: `Feature '${feature}' not available on M plan.`,
        status: 403,
        details,
        gate: normalizeGate(403, "FEAT-001", details),
      }),
    );
  }

  type ToastArgs = {
    title: string;
    description: string;
    variant?: string;
    duration?: number;
  };

  async function saveARename() {
    fireEvent.change(screen.getByDisplayValue("Demo Context"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(await screen.findByRole("button", { name: /saveChanges/ }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    return mockToast.mock.calls[0][0] as ToastArgs;
  }

  // #1646: the toast is featureGateToast's — the card notice's own copy, at
  // the same "create" scope (existing shared / public contexts keep working).
  it.each([
    ["shared_contexts", "pro", "L"],
    ["public_contexts", "promax", "XL"],
  ])(
    "%s (requires %s) → the gate copy naming %s",
    async (feature, requiredPlan, label) => {
      refuse(feature, requiredPlan);
      renderPanel(makeContext());

      const toast = await saveARename();

      expect(toast).toEqual({
        title: `plan.newTitle:${label}`,
        description: `plan.newDescription:${label}`,
        duration: 6000,
      });
    },
  );

  it("names the tier the refusal names, not FEATURE_NOTICES' hardcoded one (#1644)", async () => {
    // A deployment that moved sharing up to the top tier: the old code
    // still said "L" because the tier came from FEATURE_NOTICES.
    refuse("shared_contexts", "promax");
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.description).toBe("plan.newDescription:XL");
  });

  it("the sleep refusal (the server's sleep_mode) reads as the sleep_reports gate (#1646)", async () => {
    // Exactly what context_service raises on a tier with no Sleep
    // Maintenance: FEAT-001 under the server's own feature name.
    const details = {
      feature: "sleep_mode",
      required_plan: "pro",
      required_plan_display: "PRO",
      current_plan: "basic",
    };
    mockUpdateContext.mockRejectedValue(
      new ApiError({
        error: "FEAT-001",
        message: "Sleep Maintenance is a PRO-tier feature; upgrade your plan.",
        status: 403,
        details,
        gate: normalizeGate(403, "FEAT-001", details),
      }),
    );
    renderPanel(makeContext());

    await changeSleepModeAndSave("full");
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));

    // The whole feature, not "new ...": scope "all". The tier is the
    // deployment's label, never the server's "PRO".
    expect(mockToast.mock.calls[0][0]).toEqual({
      title: "plan.title:L",
      description: "plan.description:L",
      duration: 6000,
    });
  });

  it("the sleep cap (a quota refusal) keeps the server's own sentence (P-27)", async () => {
    const details = {
      gate: "quota",
      quota_type: "sleep_enabled_contexts",
      feature: "sleep_mode",
      current: 3,
      limit: 3,
      required_plan: null,
    };
    const message =
      "Sleep-enabled contexts quota exceeded: 4/3 in use. Contact your workspace admin to request a higher cap.";
    mockUpdateContext.mockRejectedValue(
      new ApiError({
        error: "QUOTA-001",
        message,
        status: 429,
        details,
        gate: normalizeGate(429, "QUOTA-001", details),
      }),
    );
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.title).toBe("saveFailedTitle");
    expect(toast.description).toBe(message);
  });

  it("keeps the server text when the refusal names no tier (#1644)", async () => {
    // The matrix has not answered: "no plan includes it" would be a guess.
    refuse("shared_contexts", null);
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.title).toBe("saveFailedTitle");
    expect(toast.description).toBe(
      "Feature 'shared_contexts' not available on M plan.",
    );
  });

  it("a refusal that names no tier takes the matrix's tier and display name (#1645)", async () => {
    // A server predating #1644 names no tier; the operator's matrix does —
    // the same scan the pre-check runs, labelled by the row's own name.
    mockTiers = [
      { name: "basic", display_name: "M", shared_contexts: false },
      { name: "team", display_name: "Team", shared_contexts: true },
    ] as unknown as PlanTierFeature[];
    refuse("shared_contexts", null);
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.description).toBe("plan.newDescription:Team");
  });

  it("no served tier has the feature either: the tier-less copy (#1645, #1646)", async () => {
    // The matrix has answered and no row has sharing: say so, naming no
    // tier, instead of the server's untranslated sentence.
    mockTiers = [
      { name: "basic", display_name: "M", shared_contexts: false },
    ] as unknown as PlanTierFeature[];
    refuse("shared_contexts", null);
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast).toEqual({
      title: "plan.titleNoTier",
      description: "plan.descriptionNoTier",
      duration: 6000,
    });
  });

  it("an unknown feature falls back to the server text", async () => {
    refuse("connectors", "promax");
    renderPanel(makeContext());

    const toast = await saveARename();

    expect(toast.title).toBe("saveFailedTitle");
    expect(toast.description).toBe(
      "Feature 'connectors' not available on M plan.",
    );
  });
});
