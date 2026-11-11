/**
 * MemoryHealthReport (#1225, #2764): the three-section detail of one scope.
 *
 * Pins: the request goes to the session route with the scope (a context id or
 * the 'unattributed' sentinel); structured notes render via their catalog code
 * with params interpolated and an unknown code falls back to the generic
 * message; a failed request renders the error banner; refresh refetches.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import { MemoryHealthReport, MemoryHealthStatusBadge } from "./MemoryHealthReport";

const mockGet = vi.fn();
vi.mock("@/lib/api/base", () => ({
  apiClient: { get: (...args: unknown[]) => mockGet(...args) },
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

// Stable translator: `key` or `key:{json}` so interpolation is visible. `.has`
// mirrors the catalog for the note codes under test.
const CATALOG_NOTE_KEYS = new Set(["notes.judge_failures", "notes.idle_store"]);
const stableTranslator = Object.assign(
  (key: string, values?: Record<string, unknown>) =>
    values && Object.keys(values).length > 0 ? `${key}:${JSON.stringify(values)}` : key,
  { has: (key: string) => !key.startsWith("notes.") || CATALOG_NOTE_KEYS.has(key) },
);
// Some tests hand out a NEW translator function on every call, as a provider
// re-render or a locale switch can: the fetch must not follow its identity.
let freshTranslatorPerRender = false;
vi.mock("next-intl", () => ({
  useTranslations: () =>
    freshTranslatorPerRender
      ? Object.assign(
          (key: string, values?: Record<string, unknown>) =>
            stableTranslator(key, values),
          { has: stableTranslator.has },
        )
      : stableTranslator,
  useLocale: () => "en",
}));

const CTX_ID = "11111111-2222-3333-4444-555555555555";

const DETAIL = {
  generated_at: "2026-07-12T00:00:00Z",
  context_id: CTX_ID,
  context_name: "Context A",
  overall_status: "warn",
  sections: {
    consolidation: {
      status: "warn",
      metrics: { reports_in_window: 3 },
      notes: [
        { code: "judge_failures", params: { count: 2, degraded_runs: 1 } },
        { code: "brand_new_code", params: { anything: true } },
      ],
    },
    graph: { status: "ok", metrics: { edges_by_origin: { hebbian: 10 } }, notes: [] },
    retrieval: {
      status: "ok",
      metrics: { recall_calls: 0 },
      notes: [{ code: "idle_store", params: { window_days: 7, active_memories: 12 } }],
    },
  },
};

beforeEach(() => {
  mockGet.mockReset();
  freshTranslatorPerRender = false;
});

describe("MemoryHealthReport", () => {
  it("fetches the scope from the session route and renders three sections", async () => {
    mockGet.mockResolvedValueOnce(DETAIL);
    render(<MemoryHealthReport scope={CTX_ID} />);

    await waitFor(() => {
      expect(screen.getByTestId("health-section-consolidation")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledWith(`/api/v1/memory-health?context_id=${CTX_ID}`);
    expect(screen.getByTestId("health-section-graph")).toBeInTheDocument();
    expect(screen.getByTestId("health-section-retrieval")).toBeInTheDocument();
    expect(screen.getByText("sections.consolidation.title")).toBeInTheDocument();
    expect(screen.getByText("sections.consolidation.description")).toBeInTheDocument();
  });

  it("localizes notes with params and falls back for an unknown code", async () => {
    mockGet.mockResolvedValueOnce(DETAIL);
    render(<MemoryHealthReport scope={CTX_ID} />);

    expect(
      await screen.findByText('notes.judge_failures:{"count":2,"degraded_runs":1}'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('notes.idle_store:{"window_days":7,"active_memories":12}'),
    ).toBeInTheDocument();
    expect(screen.getByText('noteUnknown:{"code":"brand_new_code"}')).toBeInTheDocument();
    // No GitHub issue reference anywhere in the rendered document.
    expect(document.body.textContent).not.toMatch(/#\d{3,}/);
  });

  it("requests the unattributed sentinel as given", async () => {
    mockGet.mockResolvedValueOnce({ ...DETAIL, context_id: null, context_name: null });
    render(<MemoryHealthReport scope="unattributed" />);

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith("/api/v1/memory-health?context_id=unattributed");
    });
  });

  it("renders the error banner when the request fails", async () => {
    mockGet.mockRejectedValueOnce(new Error("boom"));
    render(<MemoryHealthReport scope={CTX_ID} />);

    expect(await screen.findByText("loadError")).toBeInTheDocument();
    expect(screen.queryByTestId("health-section-consolidation")).toBeNull();
  });

  it("requests once per scope even when the translator changes identity", async () => {
    freshTranslatorPerRender = true;
    mockGet.mockResolvedValue(DETAIL);
    const view = render(<MemoryHealthReport scope={CTX_ID} />);
    await screen.findByTestId("health-section-consolidation");
    view.rerender(<MemoryHealthReport scope={CTX_ID} />);
    // Give a would-be refetch loop time to show itself.
    await new Promise((r) => setTimeout(r, 20));
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("refetches on refresh", async () => {
    mockGet.mockResolvedValue(DETAIL);
    render(<MemoryHealthReport scope={CTX_ID} />);
    await screen.findByTestId("health-section-consolidation");

    fireEvent.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });
});

describe("MemoryHealthStatusBadge", () => {
  it.each(["ok", "warn", "fail"])("labels %s from the catalog", (status) => {
    render(<MemoryHealthStatusBadge status={status} />);
    expect(screen.getByTestId(`health-status-${status}`)).toHaveTextContent(
      `status.${status}`,
    );
  });
});
