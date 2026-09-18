/**
 * Tests for AnalysisHistory clickable rows (#732).
 *
 * The past-runs table became row-clickable so a user can open a past run's
 * results. These pin the interactive contract the parent (AnalysesTabPanel)
 * depends on: clicking / Enter invokes onSelectRun with the row's run_id, the
 * viewed row is marked aria-current, and rows stay non-interactive when no
 * onSelectRun is supplied (backward compatible with read-only callers).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { AnalysisRunRow } from "@/lib/api/analyses";

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string) => key,
}));

import { AnalysisHistory } from "./AnalysisHistory";

function makeRun(overrides: Partial<AnalysisRunRow> = {}): AnalysisRunRow {
  return {
    run_id: "run-1",
    status: "succeeded",
    started_at: "2026-05-01T00:00:00Z",
    input_count: 10,
    cost_actual_cents: 5,
    cost_estimated_cents: 5,
    cancellation_reason: null,
    ...overrides,
  } as AnalysisRunRow;
}

describe("AnalysisHistory — clickable rows (#732)", () => {
  it("calls onSelectRun with the run_id when a row is clicked", () => {
    const onSelectRun = vi.fn();
    render(
      <AnalysisHistory
        runs={[makeRun({ run_id: "run-a" }), makeRun({ run_id: "run-b" })]}
        total={2}
        activeRunId={null}
        onSelectRun={onSelectRun}
        showCost
      />,
    );
    const rows = screen.getAllByRole("button");
    expect(rows).toHaveLength(2);
    fireEvent.click(rows[1]);
    expect(onSelectRun).toHaveBeenCalledWith("run-b");
  });

  it("triggers selection on Enter key (keyboard accessible)", () => {
    const onSelectRun = vi.fn();
    render(
      <AnalysisHistory
        runs={[makeRun({ run_id: "run-a" })]}
        total={1}
        activeRunId={null}
        onSelectRun={onSelectRun}
        showCost
      />,
    );
    fireEvent.keyDown(screen.getByRole("button"), { key: "Enter" });
    expect(onSelectRun).toHaveBeenCalledWith("run-a");
  });

  it("marks the currently-viewed row with aria-current", () => {
    render(
      <AnalysisHistory
        runs={[makeRun({ run_id: "run-a" }), makeRun({ run_id: "run-b" })]}
        total={2}
        activeRunId={null}
        selectedRunId="run-b"
        onSelectRun={vi.fn()}
        showCost
      />,
    );
    const current = screen
      .getAllByRole("button")
      .filter((el) => el.getAttribute("aria-current") === "true");
    expect(current).toHaveLength(1);
  });

  it("renders non-interactive rows when onSelectRun is omitted", () => {
    render(
      <AnalysisHistory
        runs={[makeRun()]}
        total={1}
        activeRunId={null}
        showCost
      />,
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("AnalysisHistory — cost column gate (#1571)", () => {
  it("renders the cost header and a formatted amount when showCost is true", () => {
    render(
      <AnalysisHistory
        runs={[makeRun({ cost_actual_cents: 123 })]}
        total={1}
        activeRunId={null}
        showCost
      />,
    );
    expect(screen.getByText("headerCost")).toBeInTheDocument();
    expect(screen.getByText("$1.230")).toBeInTheDocument();
  });

  it("renders no cost header and no money when showCost is false", () => {
    render(
      <AnalysisHistory
        runs={[makeRun({ cost_actual_cents: 123 })]}
        total={1}
        activeRunId={null}
        showCost={false}
      />,
    );
    expect(screen.queryByText("headerCost")).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
    // The other three columns are still there.
    expect(screen.getByText("headerMemories")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });
});
