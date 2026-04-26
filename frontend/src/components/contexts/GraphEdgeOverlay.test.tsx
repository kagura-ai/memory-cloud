/**
 * Tests for GraphEdgeOverlay (Issue #435).
 *
 * Covers:
 *   - Required metadata rows always render (source, target, type, weight)
 *   - Optional rows (created_at, confidence) only render when present
 *   - Esc key closes the overlay
 *   - Outside-click closes; inside-click does not
 *   - Close button fires onClose
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { GraphEdgeOverlay } from "./GraphEdgeOverlay";
import type { GraphEdge } from "@/lib/types/graph";

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (k: string) => k,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a", timezone: "UTC" } }),
}));

function makeEdge(overrides: Partial<GraphEdge> = {}): GraphEdge {
  return {
    source: "00000000-0000-0000-0000-000000000001",
    target: "00000000-0000-0000-0000-000000000002",
    weight: 0.75,
    type: "related_to",
    ...overrides,
  };
}

const baseProps = {
  sourceTitle: "Source memory summary",
  targetTitle: "Target memory summary",
  x: 100,
  y: 100,
  containerWidth: 800,
  containerHeight: 600,
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("GraphEdgeOverlay", () => {
  it("renders required metadata fields always", () => {
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={vi.fn()} />,
    );
    expect(screen.getByText("Source memory summary")).toBeInTheDocument();
    expect(screen.getByText("Target memory summary")).toBeInTheDocument();
    expect(screen.getByText("related_to")).toBeInTheDocument();
    expect(screen.getByText("0.75")).toBeInTheDocument();
  });

  it("omits the created_at row when the field is absent", () => {
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={vi.fn()} />,
    );
    expect(screen.queryByText("graphEdgeCreatedAt:")).not.toBeInTheDocument();
  });

  it("omits the confidence row when undefined", () => {
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={vi.fn()} />,
    );
    expect(screen.queryByText(/graphEdgeConfidence/)).not.toBeInTheDocument();
  });

  // Backend GraphEdge response model is `confidence: float | None`, so null
  // is a valid wire value. A naive `!== undefined` check would let null
  // through and crash on `.toFixed(...)`.
  it("omits the confidence row when null (not just undefined)", () => {
    render(
      <GraphEdgeOverlay
        edge={{ ...makeEdge(), confidence: null }}
        {...baseProps}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByText(/graphEdgeConfidence/)).not.toBeInTheDocument();
  });

  it("renders confidence when provided", () => {
    render(
      <GraphEdgeOverlay
        edge={makeEdge({ confidence: 0.92 })}
        {...baseProps}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("0.92")).toBeInTheDocument();
  });

  it("renders created_at when provided", () => {
    render(
      <GraphEdgeOverlay
        edge={makeEdge({ created_at: "2026-04-25T12:00:00Z" })}
        {...baseProps}
        onClose={vi.fn()}
      />,
    );
    // Don't assert exact format — the formatter is locale + timezone aware
    // and tested elsewhere. Just confirm SOMETHING with 2026 is present.
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={onClose} />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose on outside mousedown", () => {
    const onClose = vi.fn();
    render(
      <div data-testid="outside">
        <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={onClose} />
      </div>,
    );
    fireEvent.mouseDown(screen.getByTestId("outside"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not call onClose on inside mousedown", () => {
    const onClose = vi.fn();
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={onClose} />,
    );
    fireEvent.mouseDown(screen.getByText("Source memory summary"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("calls onClose when the close button is clicked", () => {
    const onClose = vi.fn();
    render(
      <GraphEdgeOverlay edge={makeEdge()} {...baseProps} onClose={onClose} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "graphEdgeClose" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
