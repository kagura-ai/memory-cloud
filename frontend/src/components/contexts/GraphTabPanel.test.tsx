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
 *   - An accepted supersede re-reads the graph so the new edge is drawn, and
 *     an older read that lands late never replaces it (#2543)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

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
// #2600: the dialog's accept is the memory's PATCH.
const mockUpdateMemoryById = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
  updateMemoryById: (...a: unknown[]) => mockUpdateMemoryById(...a),
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

// Mock useForceSimulation to expose the click callbacks and the edges it was
// last asked to draw. We use a hoisted container ref so the mock factory
// (which runs before any imports) and the test body can both reach it
// without TDZ issues.
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
    edges: [] as { source: string; target: string; type: string }[],
  },
}));

vi.mock("@/hooks/useForceSimulation", () => ({
  useForceSimulation: (input: {
    edges: readonly { source: string; target: string; type: string }[];
    onNodeClick?: (n: unknown) => void;
    onEdgeClick?: (e: unknown, x: number, y: number) => void;
  }) => {
    simCallbacks.onNodeClick =
      input.onNodeClick as typeof simCallbacks.onNodeClick;
    simCallbacks.onEdgeClick =
      input.onEdgeClick as typeof simCallbacks.onEdgeClick;
    simCallbacks.edges = [...input.edges];
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
  mockUpdateMemoryById.mockReset();
  mockReferenceMemory.mockReset();
  mockToast.mockReset();
  mockSearchParams.current = new URLSearchParams();
  simCallbacks.onNodeClick = undefined;
  simCallbacks.onEdgeClick = undefined;
  simCallbacks.edges = [];
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
  // Wait for both the simulation callback registration AND the success-branch
  // canvas mount before firing onEdgeClick. Without this gate, the callback
  // ref `setContainerEl` may not have run yet, so handleEdgeClick early-
  // returns on `containerEl=null` and the overlay never appears — a flake
  // that's invisible until the test environment changes scheduling.
  async function waitForCanvasReady() {
    await waitFor(() => {
      expect(simCallbacks.onEdgeClick).toBeDefined();
    });
    // graphVizNodeCount is the first label rendered in the canvas branch
    // (the success path) — its presence proves the success JSX has mounted
    // and any callback refs inside it (containerEl) have already fired.
    await screen.findByText("graphVizNodeCount");
  }

  it("renders the edge overlay with metadata when an edge is clicked", async () => {
    render(<GraphTabPanel contextId="ctx-1" />);
    await waitForCanvasReady();

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
    await waitForCanvasReady();

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
    await waitForCanvasReady();

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

describe("GraphTabPanel — accepted supersede (#2543)", () => {
  const NODE_C_ID = "33333333-3333-3333-3333-333333333333";

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  function withCandidate(id: string, candidateId: string): MemoryReference {
    return makeRef(id, {
      supersede_candidate: {
        memory_id: candidateId,
        summary: "Older memory",
        similarity: 0.93,
        detected_at: "2026-10-01T00:00:00Z",
        acceptance: { allowed: true },
      },
    });
  }

  // The PATCH reply after an accept: the suggestion cleared.
  function accepted(id: string): MemoryReference {
    return makeRef(id, { supersede_candidate: null });
  }

  function supersedes(source: string, target: string) {
    return {
      source,
      target,
      weight: 1,
      type: "supersedes",
      created_at: "2026-11-01T00:00:00Z",
      confidence: 1,
    };
  }

  // The accept retyped the A→B auto-edge (one row per pair and author).
  const GRAPH_AFTER_FIRST: GraphData = {
    ...SAMPLE_GRAPH,
    edges: [supersedes(NODE_A_ID, NODE_B_ID)],
  };
  const GRAPH_AFTER_SECOND: GraphData = {
    nodes: [
      ...SAMPLE_GRAPH.nodes,
      {
        id: NODE_C_ID,
        summary: "Node C summary",
        type: "note",
        importance: 0.5,
        degree: 1,
      },
    ],
    edges: [supersedes(NODE_A_ID, NODE_B_ID), supersedes(NODE_B_ID, NODE_C_ID)],
    stats: {
      total_nodes: 3,
      total_edges: 2,
      filtered_nodes: 3,
      filtered_edges: 2,
    },
  };

  function clickNode(id: string) {
    simCallbacks.onNodeClick!({
      id,
      summary: "node",
      type: "note",
      importance: 0.5,
      degree: 1,
    });
  }

  async function accept() {
    fireEvent.click(
      await screen.findByRole("button", { name: "supersede.confirm" }),
    );
  }

  async function renderGraph() {
    render(<GraphTabPanel contextId="ctx-1" />);
    await screen.findByText("graphVizNodeCount");
    await waitFor(() => expect(simCallbacks.onNodeClick).toBeDefined());
  }

  it("draws the new supersedes edge after an accept, without a reload", async () => {
    const reread = deferred<GraphData>();
    mockGetGraphData
      .mockResolvedValueOnce(SAMPLE_GRAPH)
      .mockReturnValueOnce(reread.promise);
    mockReferenceMemory.mockResolvedValue(withCandidate(NODE_A_ID, NODE_B_ID));
    mockUpdateMemoryById.mockResolvedValueOnce(accepted(NODE_A_ID));
    await renderGraph();
    expect(simCallbacks.edges.map((e) => e.type)).toEqual(["co_recall"]);

    clickNode(NODE_A_ID);
    await accept();

    await waitFor(() => expect(mockGetGraphData).toHaveBeenCalledTimes(2));
    expect(mockUpdateMemoryById).toHaveBeenCalledWith(NODE_A_ID, {
      accept_supersede_candidate: true,
    });
    expect(mockGetGraphData).toHaveBeenLastCalledWith("ctx-1", {
      limit_nodes: 200,
    });
    // The canvas stays mounted while the graph is re-read: no spinner.
    expect(screen.queryByText("graphVizLoading")).not.toBeInTheDocument();
    expect(screen.getByText("graphVizNodeCount")).toBeInTheDocument();

    await act(async () => reread.resolve(GRAPH_AFTER_FIRST));

    await waitFor(() =>
      expect(simCallbacks.edges).toEqual([
        expect.objectContaining({
          source: NODE_A_ID,
          target: NODE_B_ID,
          type: "supersedes",
        }),
      ]),
    );
  });

  it("does not re-read the graph when the accept fails", async () => {
    mockReferenceMemory.mockResolvedValue(withCandidate(NODE_A_ID, NODE_B_ID));
    mockUpdateMemoryById.mockRejectedValueOnce(new Error("boom"));
    await renderGraph();

    clickNode(NODE_A_ID);
    await accept();

    await screen.findByText("supersede.acceptFailed");
    expect(mockGetGraphData).toHaveBeenCalledTimes(1);
  });

  it("keeps the drawn graph when the re-read fails", async () => {
    const reread = deferred<GraphData>();
    mockGetGraphData
      .mockResolvedValueOnce(SAMPLE_GRAPH)
      .mockReturnValueOnce(reread.promise);
    mockReferenceMemory.mockResolvedValue(withCandidate(NODE_A_ID, NODE_B_ID));
    mockUpdateMemoryById.mockResolvedValueOnce(accepted(NODE_A_ID));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await renderGraph();

    try {
      clickNode(NODE_A_ID);
      await accept();
      await waitFor(() => expect(mockGetGraphData).toHaveBeenCalledTimes(2));
      const failure = new Error("graph down");
      await act(async () => reread.reject(failure));

      expect(screen.queryByText("graph down")).not.toBeInTheDocument();
      expect(screen.getByText("graphVizNodeCount")).toBeInTheDocument();
      expect(simCallbacks.edges.map((e) => e.type)).toEqual(["co_recall"]);
      // Silent for the user, but not lost for whoever debugs it.
      expect(warn).toHaveBeenCalledWith(expect.any(String), failure);
    } finally {
      warn.mockRestore();
    }
  });

  it("keeps the re-read when the first load lands after it (deep link)", async () => {
    const firstLoad = deferred<GraphData>();
    const reread = deferred<GraphData>();
    mockGetGraphData
      .mockReturnValueOnce(firstLoad.promise)
      .mockReturnValueOnce(reread.promise);
    mockSearchParams.current = new URLSearchParams(`memoryId=${NODE_A_ID}`);
    mockReferenceMemory.mockResolvedValue(withCandidate(NODE_A_ID, NODE_B_ID));
    mockUpdateMemoryById.mockResolvedValueOnce(accepted(NODE_A_ID));
    render(<GraphTabPanel contextId="ctx-1" />);

    // The dialog opens from the URL while the first load is still running.
    await accept();
    await waitFor(() => expect(mockGetGraphData).toHaveBeenCalledTimes(2));
    await act(async () => reread.resolve(GRAPH_AFTER_FIRST));
    // The first load was read before the accept; it lands last.
    await act(async () => firstLoad.resolve(SAMPLE_GRAPH));

    await screen.findByText("graphVizNodeCount");
    expect(simCallbacks.edges).toEqual([
      expect.objectContaining({
        source: NODE_A_ID,
        target: NODE_B_ID,
        type: "supersedes",
      }),
    ]);
  });

  it("drops an older re-read that lands after a newer one", async () => {
    const first = deferred<GraphData>();
    const second = deferred<GraphData>();
    mockGetGraphData
      .mockResolvedValueOnce(SAMPLE_GRAPH)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    mockReferenceMemory.mockImplementation(async (id: string) =>
      id === NODE_A_ID
        ? withCandidate(NODE_A_ID, NODE_B_ID)
        : withCandidate(NODE_B_ID, NODE_C_ID),
    );
    mockUpdateMemoryById.mockImplementation(async (id: string) => accepted(id));
    await renderGraph();

    clickNode(NODE_A_ID);
    await accept();
    await waitFor(() => expect(mockGetGraphData).toHaveBeenCalledTimes(2));
    clickNode(NODE_B_ID);
    await accept();
    await waitFor(() => expect(mockGetGraphData).toHaveBeenCalledTimes(3));

    await act(async () => second.resolve(GRAPH_AFTER_SECOND));
    await waitFor(() => expect(simCallbacks.edges).toHaveLength(2));
    await act(async () => first.resolve(GRAPH_AFTER_FIRST));

    expect(simCallbacks.edges).toEqual([
      expect.objectContaining({ source: NODE_A_ID, target: NODE_B_ID }),
      expect.objectContaining({ source: NODE_B_ID, target: NODE_C_ID }),
    ]);
  });
});
