/**
 * WorkspaceCreateForm — the workspace-cap refusal (#680, #1644).
 *
 * The cap is read from the gate normalised on the ApiError (`err.gate`), so a
 * current server and one predating #1644 (legacy `owned_count` / `cap`) both
 * render the localized sentence. The verbatim-English branch survives as the
 * rolling-deploy safety net for a body with no structured details at all.
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
  return { useTranslations: () => t };
});

vi.mock("next/navigation", () => {
  const router = { push: vi.fn(), back: vi.fn() };
  return { useRouter: () => router };
});

vi.mock("@/contexts/WorkspaceContext", () => {
  const value = { refreshWorkspaces: vi.fn(), switchWorkspace: vi.fn() };
  return { useWorkspace: () => value };
});

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
});

afterEach(() => cleanup());

describe("WorkspaceCreateForm — workspace cap (#1644)", () => {
  const LOCALIZED = 'workspaceLimitReachedDetailed {"owned":2,"limit":2}';

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

    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
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

    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
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

    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
  });

  it("keeps the verbatim-English safety net when there are no structured details", async () => {
    mockCreateWorkspace.mockRejectedValue(
      new ApiError({ message: SERVER_MESSAGE, status: 429, details: {} }),
    );
    await submit();

    expect(await screen.findByText(SERVER_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(/workspaceLimitReachedDetailed/)).toBeNull();
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
  });
});
