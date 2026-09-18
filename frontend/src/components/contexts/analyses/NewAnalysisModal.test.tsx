/**
 * NewAnalysisModal — estimated-cost cell gate (#1571).
 *
 * The pre-flight strip's "Estimated cost" is money: it renders only when the
 * parent passes ``showCost`` (``features.cost_display``). The memories count
 * is not money and stays either way.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string) => key,
}));

const mockPreview = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/analyses", () => ({
  previewAnalysis: (...args: unknown[]) => mockPreview(...args),
  startAnalysis: vi.fn(),
}));

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
