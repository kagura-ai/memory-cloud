/**
 * NewAnalysisModal — estimated-cost cell gate (#1571) and the preview quota
 * refusal (#1644).
 *
 * The pre-flight strip's "Estimated cost" is money: it renders only when the
 * parent passes ``showCost`` (``features.cost_display``). The memories count
 * is not money and stays either way.
 *
 * The daily analysis quota is read from the normalised gate on the ApiError
 * (``err.gate``), so a current server and one predating #1644 render the
 * same localized sentence.
 *
 * #1646 (A2): the footer no longer reveals the rollout allowlist.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

// ICU params are echoed after the key so the counts' flow is visible.
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns: string) => (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
}));

const mockPreview = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/analyses", () => ({
  previewAnalysis: (...args: unknown[]) => mockPreview(...args),
  startAnalysis: vi.fn(),
}));

import { ApiError } from "@/lib/api/base";
import { normalizeGate } from "@/lib/gates/featureGates";
import { NewAnalysisModal } from "./NewAnalysisModal";

function renderModal(showCost: boolean) {
  return render(
    <NewAnalysisModal
      open
      contextId="ctx-1"
      contextName="My context"
      onClose={vi.fn()}
      onStarted={vi.fn()}
      showCost={showCost}
    />,
  );
}

beforeEach(() => {
  mockPreview.mockReset();
  mockPreview.mockResolvedValue({
    memory_count: 12,
    cluster_count_estimate: 3,
    estimated_cost_cents: 42,
    model_id: "gpt-5-nano",
    breakdown: { input_tokens: 1, output_tokens: 1, calls: 1 },
  });
});

afterEach(() => cleanup());

describe("NewAnalysisModal — estimated cost gate (#1571)", () => {
  it("shows the estimated-cost cell when showCost is true", async () => {
    renderModal(true);
    expect(await screen.findByText("$0.420")).toBeInTheDocument();
    expect(screen.getByText("preflight.estimatedCost")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("renders no estimated-cost cell and no money when showCost is false", async () => {
    renderModal(false);
    // Wait for the debounced preview to land (memories count is still shown).
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.queryByText("preflight.estimatedCost")).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });
});

describe("NewAnalysisModal — the footer keeps the allowlist silent (#1646)", () => {
  it("renders no footer hint, so nothing names the rollout allowlist", async () => {
    renderModal(false);
    // Let the preview land so the whole modal is on screen.
    expect(await screen.findByText("12")).toBeInTheDocument();

    // The footer holds its two actions and nothing else.
    const footer = screen.getByRole("button", { name: "cancel" }).parentElement;
    expect(
      Array.from(footer?.children ?? []).map((el) => el.textContent),
    ).toEqual(["cancel", "submit"]);
    expect(document.body.textContent).not.toMatch(/allowlist/i);
  });
});

describe("NewAnalysisModal — preview quota refusal (#1644)", () => {
  /** An ApiError exactly as lib/api/base.ts would build it from this body. */
  function refusal(
    status: number,
    error: string,
    message: string,
    details: Record<string, unknown>,
  ): ApiError {
    return new ApiError({
      error,
      message,
      status,
      details,
      gate: normalizeGate(status, error, details),
    });
  }

  const LOCALIZED =
    'errors.QUOTA-001 {"used":"3","limit":"3","addon":"1","resetsAt":"2026-09-24T00:00:00+09:00"}';

  it("localizes the analysis quota from err.gate on a current server", async () => {
    mockPreview.mockReset();
    mockPreview.mockRejectedValue(
      refusal(429, "QUOTA-001", "Analysis daily quota exceeded: 3/3", {
        gate: "quota",
        quota_type: "memory_analysis",
        current: 3,
        limit: 3,
        used_today: 3,
        limit_today: 3,
        addon_bonus: 1,
        remaining_today: 0,
        resets_at: "2026-09-24T00:00:00+09:00",
      }),
    );
    renderModal(false);

    // Rendered as-is: no "could not load the preview" prefix on a refusal.
    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
  });

  it("localizes the same sentence against a server predating #1644", async () => {
    mockPreview.mockReset();
    mockPreview.mockRejectedValue(
      refusal(429, "QUOTA-001", "Analysis daily quota exceeded: 3/3", {
        quota_type: "memory_analysis",
        used_today: 3,
        limit_today: 3,
        addon_bonus: 1,
        remaining_today: 0,
        resets_at: "2026-09-24T00:00:00+09:00",
      }),
    );
    renderModal(false);

    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
  });

  it("reads the canonical counts, not the legacy names", async () => {
    // What the modal consumes is err.gate: a body carrying only the
    // canonical current / limit still localizes.
    mockPreview.mockReset();
    mockPreview.mockRejectedValue(
      refusal(429, "QUOTA-001", "Analysis daily quota exceeded: 3/3", {
        gate: "quota",
        quota_type: "memory_analysis",
        current: 3,
        limit: 3,
        addon_bonus: 1,
        resets_at: "2026-09-24T00:00:00+09:00",
      }),
    );
    renderModal(false);

    expect(await screen.findByText(LOCALIZED)).toBeInTheDocument();
  });

  it("does not dress another quota up as the analysis quota", async () => {
    // The daily REST cap is also QUOTA-001, with no analysis counts at all.
    mockPreview.mockReset();
    mockPreview.mockRejectedValue(
      refusal(429, "QUOTA-001", "REST API daily quota exceeded", {
        gate: "quota",
        quota_type: "api_rest_daily",
        retry_after: 86400,
      }),
    );
    renderModal(false);

    expect(
      await screen.findByText("REST API daily quota exceeded"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/errors\.QUOTA-001/)).toBeNull();
  });

  it("prefixes a non-refusal preview failure", async () => {
    mockPreview.mockReset();
    mockPreview.mockRejectedValue(
      new ApiError({ message: "upstream timeout", status: 502 }),
    );
    renderModal(false);

    expect(
      await screen.findByText("errorLoadingPreview: upstream timeout"),
    ).toBeInTheDocument();
  });
});
