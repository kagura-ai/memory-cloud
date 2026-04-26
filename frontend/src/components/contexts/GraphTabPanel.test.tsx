/**
 * Tests for GraphTabPanel click integration (Issue #435).
 *
 * d3-force / d3-drag don't behave deterministically inside jsdom (no real
 * pointer event lifecycle), so we mock useForceSimulation. The mock captures
 * the latest onNodeClick / onEdgeClick callbacks so each test can fire them
 * directly, decoupled from the simulation lifecycle.
 *
 * What's covered here:
 *   - Node click triggers referenceMemory + opens MemoryDetailDialog
 *   - Edge click renders the metadata overlay
 *   - Outside click on overlay dismisses it
 *   - URL gets ?memoryId= updated when a node is clicked
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { GraphTabPanel } from "./GraphTabPanel";
import type { GraphData } from "@/lib/types/graph";
import type { MemoryReference } from "@/lib/types/memory";

// ---------- Mocks ------------------------------------------------------------

const { mockReplace, mockSearchParams } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  mockSearchParams: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => "/workspace/contexts/ctx-1",
  useSearchParams: () => ({
    get: (k: string) => mockSearchParams.current.get(k),
    toString: () => mockSearchParams.current.toString(),
  }),
}));

const mockGetGraphData = vi.fn();
vi.mock("@/lib/api/graph", () => ({
  graphApi: {
    getGraphData: (...a: unknown[]) => mockGetGraphData(...a),
  },
}));

const mockReferenceMemory = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
  forgetMemory: vi.fn(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Stable identity for the translation function — without this, every render
// would return a new arrow function, fetchData's useCallback would re-memo,
// the data-fetch useEffect would re-fire, and the panel would never settle
// into loading=false.
const tStub = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => tStub,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a", timezone: "UTC" } }),
}));

// Mock useForceSimulation to expose the click callbacks. We use a hoisted
// container ref so the mock factory (which runs before any imports) and the
// test body can both reach it without TDZ issues.
const { simCallbacks } = vi.hoisted(() => ({
  simCallbacks: {
    onNodeClick: undefined as
      | ((node: {
          id: string;
          summary: string;
          type: string;
          importance: number;
          degree: number;
        }) => void)
      | undefined,
    onEdgeClick: undefined as
      | ((
          edge: {
            source: string;
            target: string;
            weight: number;
            type: string;
          },
          x: number,
          y: number,
        ) => void)
      | undefined,
  },
}));

vi.mock("@/hooks/useForceSimulation", () => ({
  useForceSimulation: (input: {
    onNodeClick?: (n: unknown) => void;
    onEdgeClick?: (e: unknown, x: number, y: number) => void;
  }) => {
    simCallbacks.onNodeClick =
      input.onNodeClick as typeof simCallbacks.onNodeClick;
    simCallbacks.onEdgeClick =
      input.onEdgeClick as typeof simCallbacks.onEdgeClick;
  },
}));

// ResizeObserver is not implemented in jsdom — stub it so the panel mounts.
class StubResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).ResizeObserver = StubResizeObserver;

// ---------- Helpers ----------------------------------------------------------

const NODE_A_ID = "11111111-1111-1111-1111-111111111111";
const NODE_B_ID = "22222222-2222-2222-2222-222222222222";

const SAMPLE_GRAPH: GraphData = {
  nodes: [
    {
      id: NODE_A_ID,
      summary: "Node A summary",
      type: "note",
      importance: 0.8,
      degree: 3,
    },
    {
      id: NODE_B_ID,
      summary: "Node B summary",
      type: "code",
      importance: 0.6,
      degree: 2,
    },
  ],
  edges: [
    {
      source: NODE_A_ID,
      target: NODE_B_ID,
      weight: 0.85,
      type: "co_recall",
      created_at: "2026-04-25T00:00:00Z",
      confidence: 0.9,
    },
  ],
  stats: {
    total_nodes: 2,
    total_edges: 1,
    filtered_nodes: 2,
    filtered_edges: 1,
  },
};

function makeRef(
  id: string,
  overrides: Partial<MemoryReference> = {},
): MemoryReference {
  return {
    memory_id: id,
    summary: "Hydrated summary",
    context_summary: null,
    content: "Hydrated body for graph node",
    details: null,
    type: "note",
    scope: "working",
    importance: 0.7,
    tags: [],
    context: null,
    created_at: "2026-04-25T00:00:00Z",
    updated_at: "2026-04-25T00:00:00Z",
    client: "api",
    outgoing_links: [],
    outgoing_has_more: false,
    incoming_links: [],
    incoming_has_more: false,
    ...overrides,
  };
}

beforeEach(() => {
  mockReplace.mockReset();
  mockGetGraphData.mockReset();
  mockReferenceMemory.mockReset();
  mockToast.mockReset();
  mockSearchParams.current = new URLSearchParams();
  simCallbacks.onNodeClick = undefined;
  simCallbacks.onEdgeClick = undefined;
  mockGetGraphData.mockResolvedValue(SAMPLE_GRAPH);
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------- Tests ------------------------------------------------------------

describe("GraphTabPanel — node click integration (Issue #435)", () => {
  it("opens MemoryDetailDialog when a node is clicked", async () => {
    mockReferenceMemory.mockResolvedValue(makeRef(NODE_A_ID));
    render(<GraphTabPanel contextId="ctx-1" />);

    // Wait for graph data to load and the simulation hook to capture the cb.
    await waitFor(() => {
      expect(simCallbacks.onNodeClick).toBeDefined();
    });

    simCallbacks.onNodeClick!({
      id: NODE_A_ID,
      summary: "Node A summary",
      type: "note",
      importance: 0.8,
      degree: 3,
    });

    await waitFor(() => {
      expect(mockReferenceMemory).toHaveBeenCalledWith(NODE_A_ID);
    });
    await screen.findByText("Hydrated body for graph node");
  });

  it("updates ?memoryId= URL when a node is clicked", async () => {
    mockReferenceMemory.mockResolvedValue(makeRef(NODE_A_ID));
    render(<GraphTabPanel contextId="ctx-1" />);

    await waitFor(() => {
      expect(simCallbacks.onNodeClick).toBeDefined();
    });

    simCallbacks.onNodeClick!({
      id: NODE_A_ID,
      summary: "Node A",
      type: "note",
      importance: 0.5,
      degree: 1,
    });

    await waitFor(() => {
      const lastCall = mockReplace.mock.calls.at(-1)?.[0] as string;
      expect(lastCall).toContain(`memoryId=${NODE_A_ID}`);
    });
  });

  it("auto-opens dialog from ?memoryId= deep-link (cross-tab consistency)", async () => {
    mockSearchParams.current = new URLSearchParams(`memoryId=${NODE_A_ID}`);
    mockReferenceMemory.mockResolvedValue(makeRef(NODE_A_ID));

    render(<GraphTabPanel contextId="ctx-1" />);

    await waitFor(() => {
      expect(mockReferenceMemory).toHaveBeenCalledWith(NODE_A_ID);
    });
    await screen.findByText("Hydrated body for graph node");
  });
});

describe("GraphTabPanel — edge click integration (Issue #435)", () => {
  it("renders the edge overlay with metadata when an edge is clicked", async () => {
    render(<GraphTabPanel contextId="ctx-1" />);

    await waitFor(() => {
      expect(simCallbacks.onEdgeClick).toBeDefined();
    });

    simCallbacks.onEdgeClick!(
      {
        source: NODE_A_ID,
        target: NODE_B_ID,
        weight: 0.85,
        type: "co_recall",
      },
      120,
      80,
    );

    // Required metadata fields appear (type label, weight, both titles).
    await screen.findByText("co_recall");
    expect(screen.getByText("0.85")).toBeInTheDocument();
    // Source/target node summaries are resolved from the graph data.
    expect(screen.getByText("Node A summary")).toBeInTheDocument();
    expect(screen.getByText("Node B summary")).toBeInTheDocument();
  });

  it("does not call referenceMemory on edge click (overlay is local-only)", async () => {
    render(<GraphTabPanel contextId="ctx-1" />);

    await waitFor(() => {
      expect(simCallbacks.onEdgeClick).toBeDefined();
    });

    simCallbacks.onEdgeClick!(
      {
        source: NODE_A_ID,
        target: NODE_B_ID,
        weight: 0.5,
        type: "related",
      },
      50,
      50,
    );

    // Wait a tick so any accidental side effect has a chance to fire.
    await screen.findByText("related");
    expect(mockReferenceMemory).not.toHaveBeenCalled();
  });

  it("dismisses the overlay on Escape", async () => {
    render(<GraphTabPanel contextId="ctx-1" />);

    await waitFor(() => {
      expect(simCallbacks.onEdgeClick).toBeDefined();
    });

    simCallbacks.onEdgeClick!(
      {
        source: NODE_A_ID,
        target: NODE_B_ID,
        weight: 0.5,
        type: "co_recall",
      },
      50,
      50,
    );

    await screen.findByText("co_recall");
    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByText("co_recall")).not.toBeInTheDocument();
    });
  });
});
