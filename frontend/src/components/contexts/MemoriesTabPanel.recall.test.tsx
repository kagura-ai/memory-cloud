/**
 * MemoriesTabPanel recall mode (#2132).
 *
 * Covers: the mode control (text match stays the default; switching to recall
 * does not run a search by itself), submitting a recall sends the context and
 * tag filters with include_superseded=false, the labelled "include older
 * versions" switch re-runs the same query with include_superseded=true, and a
 * folded version's button opens that memory in the detail dialog through the
 * ?memoryId= deep link.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemoriesTabPanel } from "./MemoriesTabPanel";

const { mockReplace, mockSearchParams } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  mockSearchParams: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => "/workspace/contexts/ctx-1",
  useSearchParams: () => ({
    get: (k: string) => mockSearchParams.current.get(k),
    getAll: (k: string) => mockSearchParams.current.getAll(k),
    toString: () => mockSearchParams.current.toString(),
  }),
}));

const mockGetMemories = vi.fn();
const mockReferenceMemory = vi.fn();
const mockRecallMemories = vi.fn();
// #2600: the dialog's accept is the memory's PATCH.
const mockUpdateMemoryById = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  getMemories: (...a: unknown[]) => mockGetMemories(...a),
  referenceMemory: (...a: unknown[]) => mockReferenceMemory(...a),
  recallMemories: (...a: unknown[]) => mockRecallMemories(...a),
  updateMemoryById: (...a: unknown[]) => mockUpdateMemoryById(...a),
  forgetMemory: vi.fn(),
}));

vi.mock("@/components/contexts/TagCloud", () => ({
  TagCloud: () => null,
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const { stableT } = vi.hoisted(() => ({
  stableT: (k: string, values?: Record<string, unknown>) => {
    if (!values) return k;
    const parts = Object.values(values).map((v) => String(v));
    return parts.length > 0 ? `${k}:${parts.join(",")}` : k;
  },
}));
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a", timezone: "UTC" } }),
}));

const NEWEST = "aaaaaaaa-0000-0000-0000-000000000001";
const OLDER = "bbbbbbbb-0000-0000-0000-000000000002";

function recallRow(overrides: Record<string, unknown> = {}) {
  return {
    memory_id: NEWEST,
    summary: "Deploy target is region B",
    context_summary: null,
    type: "fact",
    importance: 0.6,
    scope: "persistent",
    created_at: "2026-10-01T00:00:00Z",
    client: "api",
    tags: [],
    context: null,
    score: 0.5,
    ...overrides,
  };
}

beforeEach(() => {
  // Reset, not just clear: an unconsumed mock*Once must not leak across tests.
  mockRecallMemories.mockReset();
  mockSearchParams.current = new URLSearchParams();
  mockGetMemories.mockResolvedValue({
    memories: [
      {
        id: "99999999-9999-9999-9999-999999999999",
        summary: "Listed row",
        type: "note",
        scope: "working",
        importance: 0.4,
        created_at: "2026-04-25T00:00:00Z",
        updated_at: "2026-04-25T00:00:00Z",
      },
    ],
    total: 1,
    has_more: false,
  });
  mockRecallMemories.mockResolvedValue({
    results: [recallRow({ folded: { count: 1, memory_ids: [OLDER] } })],
  });
  mockReferenceMemory.mockReturnValue(new Promise(() => {}));
});

afterEach(() => {
  vi.clearAllMocks();
});

async function enterRecallMode() {
  render(<MemoriesTabPanel contextId="ctx-1" />);
  await screen.findByText("Listed row");
  fireEvent.click(screen.getByRole("button", { name: "modeRecall" }));
}

function submitQuery(text: string) {
  fireEvent.change(screen.getByRole("searchbox", { name: "search.label" }), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole("button", { name: "submit" }));
}

describe("MemoriesTabPanel — recall mode (#2132)", () => {
  it("keeps text match as the default and does not recall on mode switch", async () => {
    render(<MemoriesTabPanel contextId="ctx-1" />);
    await screen.findByText("Listed row");
    expect(screen.getByRole("button", { name: "modeText" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("switch")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "modeRecall" }));
    expect(screen.getByRole("button", { name: "modeRecall" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(mockRecallMemories).not.toHaveBeenCalled();
  });

  it("submits a recall with the context filter and include_superseded=false", async () => {
    await enterRecallMode();
    submitQuery("  deploy target  ");
    await screen.findByText("Deploy target is region B");
    expect(mockRecallMemories).toHaveBeenCalledTimes(1);
    expect(mockRecallMemories).toHaveBeenCalledWith({
      query: "deploy target",
      k: 20,
      include_superseded: false,
      filters: { context_id: "ctx-1" },
    });
    expect(screen.getByText("folded:1")).toBeInTheDocument();
  });

  it("sends the selected tags as an ALL-match recall filter", async () => {
    mockSearchParams.current = new URLSearchParams("tags=a&tags=b");
    await enterRecallMode();
    submitQuery("deploy");
    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalled());
    expect(mockRecallMemories).toHaveBeenCalledWith(
      expect.objectContaining({
        filters: { context_id: "ctx-1", tags: ["a", "b"], tags_match: "all" },
      }),
    );
  });

  it("re-runs the query with include_superseded=true when the switch turns on", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    const toggle = screen.getByRole("switch", { name: "includeSuperseded" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalledTimes(2));
    expect(mockRecallMemories).toHaveBeenLastCalledWith({
      query: "deploy",
      k: 20,
      include_superseded: true,
      filters: { context_id: "ctx-1" },
    });
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("does not recall when the switch flips before any query ran", async () => {
    await enterRecallMode();
    fireEvent.click(screen.getByRole("switch", { name: "includeSuperseded" }));
    expect(mockRecallMemories).not.toHaveBeenCalled();
  });

  it("opens a folded version in the detail dialog via ?memoryId=", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    const link = await screen.findByRole("button", {
      name: `openFolded:${OLDER}`,
    });
    fireEvent.click(link);
    await waitFor(() => expect(mockReferenceMemory).toHaveBeenCalled());
    expect(mockReferenceMemory.mock.calls[0][0]).toBe(OLDER);
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining(`memoryId=${OLDER}`),
    );
  });

  it("hides the recall controls and spends no recall back in text mode", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    fireEvent.click(screen.getByRole("button", { name: "modeText" }));
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: "submit" })).toBeNull();
    expect(await screen.findByText("Listed row")).toBeInTheDocument();
    expect(mockRecallMemories).toHaveBeenCalledTimes(1);
  });

  it("shows the last rows again on Text -> Recall without a new recall", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    fireEvent.click(screen.getByRole("button", { name: "modeText" }));
    await screen.findByText("Listed row");
    fireEvent.click(screen.getByRole("button", { name: "modeRecall" }));
    expect(screen.getByText("Deploy target is region B")).toBeInTheDocument();
    expect(mockRecallMemories).toHaveBeenCalledTimes(1);
  });

  it("keeps an in-flight recall across Text -> Recall and shows its response", async () => {
    let resolve: (v: unknown) => void = () => {};
    mockRecallMemories.mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    await enterRecallMode();
    submitQuery("deploy");
    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "modeText" }));
    await screen.findByText("Listed row");
    fireEvent.click(screen.getByRole("button", { name: "modeRecall" }));
    expect(mockRecallMemories).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolve({ results: [recallRow()] });
    });
    expect(
      await screen.findByText("Deploy target is region B"),
    ).toBeInTheDocument();
    expect(mockRecallMemories).toHaveBeenCalledTimes(1);
  });

  it("drops an obsolete response when the switch changed the request", async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    mockRecallMemories
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveFirst = r;
        }),
      )
      .mockResolvedValueOnce({
        results: [recallRow({ memory_id: OLDER, summary: "Billing row" })],
      });
    await enterRecallMode();
    submitQuery("deploy");
    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("switch", { name: "includeSuperseded" }));
    await screen.findByText("Billing row");
    expect(mockRecallMemories).toHaveBeenLastCalledWith(
      expect.objectContaining({ include_superseded: true }),
    );
    await act(async () => {
      resolveFirst({ results: [recallRow()] });
    });
    expect(screen.getByText("Billing row")).toBeInTheDocument();
    expect(screen.queryByText("Deploy target is region B")).toBeNull();
  });

  it("does not show the previous query's rows while a re-run is in flight", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    mockRecallMemories.mockReturnValueOnce(new Promise(() => {}));
    submitQuery("billing");
    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Deploy target is region B")).toBeNull();
    expect(screen.getByText("loading")).toBeInTheDocument();
  });

  it("does not refetch the list while recall mode is shown", async () => {
    await enterRecallMode();
    const listCalls = mockGetMemories.mock.calls.length;
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    expect(mockGetMemories.mock.calls.length).toBe(listCalls);
  });

  it("re-runs the submitted recall after a supersede is accepted (#2515)", async () => {
    await enterRecallMode();
    submitQuery("deploy");
    await screen.findByText("Deploy target is region B");
    const listCalls = mockGetMemories.mock.calls.length;
    const newest = {
      memory_id: NEWEST,
      summary: "Deploy target is region B",
      context_summary: null,
      content: "body",
      details: null,
      type: "fact",
      scope: "persistent",
      importance: 0.6,
      tags: [],
      context: null,
      created_at: "2026-10-01T00:00:00Z",
      updated_at: "2026-10-01T00:00:00Z",
      client: "api",
    };
    mockReferenceMemory.mockResolvedValueOnce({
      ...newest,
      supersede_candidate: {
        memory_id: OLDER,
        summary: "Deploy target is region A",
        similarity: 0.93,
        detected_at: "2026-10-01T00:00:00Z",
        acceptance: { allowed: true },
      },
    });
    // The PATCH reply: the suggestion cleared.
    mockUpdateMemoryById.mockResolvedValueOnce({
      ...newest,
      supersede_candidate: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "view" }));

    fireEvent.click(
      await screen.findByRole("button", { name: "supersede.confirm" }),
    );

    await waitFor(() => expect(mockRecallMemories).toHaveBeenCalledTimes(2));
    expect(mockUpdateMemoryById).toHaveBeenCalledWith(NEWEST, {
      accept_supersede_candidate: true,
    });
    expect(mockRecallMemories).toHaveBeenLastCalledWith({
      query: "deploy",
      k: 20,
      include_superseded: false,
      filters: { context_id: "ctx-1" },
    });
    // List mode stays as it was: the accept adds no list fetch.
    expect(mockGetMemories.mock.calls.length).toBe(listCalls);
  });

  it("shows an error banner when recall fails", async () => {
    mockRecallMemories.mockRejectedValueOnce(new Error("boom"));
    await enterRecallMode();
    submitQuery("deploy");
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
