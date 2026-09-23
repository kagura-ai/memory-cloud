/**
 * WorkspaceCreateForm — the workspace-cap refusal (#680, #1644).
 *
 * The cap is read from the gate normalised on the ApiError (`err.gate`), so a
 * current server and one predating #1644 (legacy `owned_count` / `cap`) both
 * render the localized notice. A body with no structured details at all is
 * an ordinary create failure: no notice, the generic error line.
 *
 * #1646 Q6: the cap renders through FeatureGateNotice (`gate.quota.*`) above
 * the form, lifted by `useErrorGate(err, "workspaces")`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

// A stable translator that echoes ICU params, so the counts' flow is visible.
vi.mock("next-intl", () => {
  const t = (key: string, params?: Record<string, unknown>) =>
    params ? `${key} ${JSON.stringify(params)}` : key;
  return { useTranslations: () => t, useLocale: () => "en" };
});

const mockPush = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => {
  const router = { push: mockPush, back: vi.fn() };
  return { useRouter: () => router };
});

// useErrorGate reads the member's role (for canUpgrade) from the workspace.
let mockRole = "owner";
vi.mock("@/contexts/WorkspaceContext", () => {
  const actions = { refreshWorkspaces: vi.fn(), switchWorkspace: vi.fn() };
  return {
    useWorkspace: () => ({
      ...actions,
      currentWorkspace: { id: "ws-1", current_user_role: mockRole },
      loading: false,
    }),
  };
});

// /system/info (the Plan page flag) and the shared tier matrix, which
// useErrorGate subscribes to. Without these the real hooks fetch in jsdom.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => null,
}));

vi.mock("@/hooks/use-toast", () => {
  const value = { toast: vi.fn() };
  return { useToast: () => value };
});

const mockCreateWorkspace = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/workspaces", () => ({
  createWorkspace: (...args: unknown[]) => mockCreateWorkspace(...args),
}));

import { ApiError } from "@/lib/api/base";
import { normalizeGate } from "@/lib/gates/featureGates";
import { WorkspaceCreateForm } from "./WorkspaceCreateForm";

const SERVER_MESSAGE =
  "Workspace limit reached: you currently own 2 workspace(s) (cap: 2).";

/** An ApiError exactly as lib/api/base.ts would build it from this body. */
function refusal(details: Record<string, unknown>): ApiError {
  return new ApiError({
    error: "QUOTA-001",
    message: SERVER_MESSAGE,
    status: 429,
    details,
    gate: normalizeGate(429, "QUOTA-001", details),
  });
}

async function submit() {
  render(<WorkspaceCreateForm onSuccess={vi.fn()} onCancel={vi.fn()} />);
  fireEvent.change(screen.getByLabelText(/workspaceName/), {
    target: { value: "Acme" },
  });
  // Inside act so the rejected create and the `finally` that follows it
  // settle before the assertions run.
  await act(async () => {
    fireEvent.submit(screen.getByLabelText(/workspaceName/).closest("form")!);
  });
}

beforeEach(() => {
  mockCreateWorkspace.mockReset();
  mockPush.mockReset();
  mockRole = "owner";
  mockFeatures = { plan_page: true };
});

afterEach(() => cleanup());

/** The cap notice, once it renders (FeatureGateNotice's inline Alert). */
async function findCapNotice() {
  return screen.findByRole("alert");
}

describe("WorkspaceCreateForm — workspace cap (#1644)", () => {
  // The counts reach the gate copy, keyed on the canonical names.
  const COUNTS = '"current":2,"limit":2';

  it("localizes the cap from err.gate on a current server", async () => {
    mockCreateWorkspace.mockRejectedValue(
      refusal({
        gate: "quota",
        quota_type: "workspace_limit_reached",
        current: 2,
        limit: 2,
        required_plan: "basic",
        required_plan_display: "M",
        current_plan: "free",
        owned_count: 2,
        cap: 2,
        tier: "free",
        next_tier: "basic",
      }),
    );
    await submit();

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent("quota.title");
    expect(notice).toHaveTextContent(/quota\.description \{/);
    expect(notice).toHaveTextContent(COUNTS);
    expect(notice).toHaveTextContent(
      '"feature":"features.workspaces.singular"',
    );
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
  });

  it("localizes the cap from the canonical counts alone", async () => {
    mockCreateWorkspace.mockRejectedValue(
      refusal({
        gate: "quota",
        quota_type: "workspace_limit_reached",
        current: 2,
        limit: 2,
      }),
    );
    await submit();

    // No current plan on the wire: the plan-free sentence, same counts.
    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.descriptionNoPlan \{/);
    expect(notice).toHaveTextContent(COUNTS);
  });

  it("still localizes against a pre-#1644 body carrying only owned_count/cap", async () => {
    mockCreateWorkspace.mockRejectedValue(
      refusal({
        quota_type: "workspace_limit_reached",
        owned_count: 2,
        cap: 2,
        tier: "free",
        next_tier: "basic",
      }),
    );
    await submit();

    expect(await findCapNotice()).toHaveTextContent(COUNTS);
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
  });

  it("treats a refusal with no structured details as an ordinary create failure", async () => {
    mockCreateWorkspace.mockRejectedValue(
      new ApiError({ message: SERVER_MESSAGE, status: 429, details: {} }),
    );
    await submit();

    // No gate, so no notice: the generic line, which still carries the
    // server's text.
    expect(
      await screen.findByText(`failedToCreateWorkspace: ${SERVER_MESSAGE}`),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not render another quota as the workspace cap", async () => {
    mockCreateWorkspace.mockRejectedValue(
      new ApiError({
        error: "QUOTA-001",
        message: "REST API daily quota exceeded",
        status: 429,
        gate: { state: "quota", quotaType: "api_rest_daily" },
      }),
    );
    await submit();

    expect(
      await screen.findByText(
        "failedToCreateWorkspace: REST API daily quota exceeded",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("WorkspaceCreateForm — the cap notice (#1646 Q6)", () => {
  const CURRENT_SERVER = {
    gate: "quota",
    quota_type: "workspace_limit_reached",
    current: 2,
    limit: 2,
    required_plan: "basic",
    required_plan_display: "M",
    current_plan: "free",
  };

  it("sits above the form, not inside it", async () => {
    mockCreateWorkspace.mockRejectedValue(refusal(CURRENT_SERVER));
    await submit();

    const notice = await findCapNotice();
    const form = screen.getByLabelText(/workspaceName/).closest("form")!;
    expect(form.contains(notice)).toBe(false);
    // The card body's first child, directly followed by the form.
    expect(notice.parentElement!.firstElementChild).toBe(notice);
    expect(notice.nextElementSibling?.tagName).toBe("FORM");
  });

  it("owner on a Plan-page deployment: the tier that raises the cap and a CTA to the Plan page", async () => {
    mockCreateWorkspace.mockRejectedValue(refusal(CURRENT_SERVER));
    await submit();

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.upsell \{"plan":"M"/);
    fireEvent.click(screen.getByRole("button", { name: /^quota\.action/ }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it.each([
    ["an admin", "admin", { plan_page: true }],
    ["an owner with the Plan page off", "owner", {}],
  ] as const)(
    "%s: the notice stays, with no upsell and no CTA",
    async (_l, role, info) => {
      mockRole = role;
      mockFeatures = { ...info };
      mockCreateWorkspace.mockRejectedValue(refusal(CURRENT_SERVER));
      await submit();

      const notice = await findCapNotice();
      expect(notice).toHaveTextContent("quota.title");
      expect(notice.textContent).not.toContain("quota.upsell");
      expect(
        screen.queryByRole("button", { name: /^quota\.action/ }),
      ).toBeNull();
    },
  );

  it("a cap refusal without counts renders the count-free sentence, not the server English", async () => {
    mockCreateWorkspace.mockRejectedValue(
      refusal({ gate: "quota", quota_type: "workspace_limit_reached" }),
    );
    await submit();

    const notice = await findCapNotice();
    expect(notice).toHaveTextContent(/quota\.descriptionNoNumbers/);
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
  });

  it("a new attempt clears the notice", async () => {
    mockCreateWorkspace.mockRejectedValueOnce(refusal(CURRENT_SERVER));
    await submit();
    expect(await findCapNotice()).toBeInTheDocument();

    mockCreateWorkspace.mockRejectedValueOnce(
      new ApiError({ message: "Invalid name", status: 422, details: {} }),
    );
    await act(async () => {
      fireEvent.submit(screen.getByLabelText(/workspaceName/).closest("form")!);
    });
    expect(screen.getByText("validationError")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
