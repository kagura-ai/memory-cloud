/**
 * RecallResultsList (#2132): the Memories tab's recall-mode result rows.
 *
 * Covers a row without `folded` (no folded line), a row whose older versions
 * recall folded under it (count + one focusable button per folded id, each
 * opening that id), a row shadowed by a `supersedes` edge (badge + a button
 * opening the newer memory), the per-row View button and the empty state.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecallResultsList } from "./RecallResultsList";
import type { RecallResultItem } from "@/lib/types/memory";
import { referenceMemory } from "@/lib/api/memory";
vi.mock("@/lib/api/memory", () => ({ referenceMemory: vi.fn() }));
beforeEach(() => { vi.mocked(referenceMemory).mockReset(); });

const { stableT } = vi.hoisted(() => ({
  stableT: (k: string, values?: Record<string, unknown>) => {
    if (!values) return k;
    const parts = Object.values(values).map((v) => String(v));
    return `${k}:${parts.join(",")}`;
  },
}));
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
  useLocale: () => "en",
}));

const NEWEST = "aaaaaaaa-0000-0000-0000-000000000001";
const OLDER_1 = "bbbbbbbb-0000-0000-0000-000000000002";
const OLDER_2 = "cccccccc-0000-0000-0000-000000000003";
const NEWER = "dddddddd-0000-0000-0000-000000000004";

function row(overrides: Partial<RecallResultItem> = {}): RecallResultItem {
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
    score: 0.8123,
    ...overrides,
  };
}

describe("RecallResultsList", () => {
  it("renders a row without folded versions and no folded line", () => {
    const onOpen = vi.fn();
    render(
      <RecallResultsList
        results={[row()]}
        loading={false}
        query="deploy"
        onOpen={onOpen}
      />,
    );
    const item = screen.getByRole("listitem");
    expect(
      within(item).getByText("Deploy target is region B"),
    ).toBeInTheDocument();
    expect(within(item).getByText("score:81")).toBeInTheDocument();
    expect(within(item).queryByText(/^folded:/)).not.toBeInTheDocument();
    expect(
      within(item).queryByRole("button", { name: /^openFolded:/ }),
    ).toBeNull();
    expect(within(item).queryByText("superseded")).not.toBeInTheDocument();
  });

  it("treats folded: null like no folded versions", () => {
    render(
      <RecallResultsList
        results={[row({ folded: null })]}
        loading={false}
        query="deploy"
        onOpen={vi.fn()}
      />,
    );
    expect(screen.queryByText(/^folded:/)).not.toBeInTheDocument();
  });

  it("loads summaries only when expanded, without recording access, and opens each memory", async () => {
    vi.mocked(referenceMemory).mockImplementation(async (id) => ({ summary: `Summary ${id}` }) as Awaited<ReturnType<typeof referenceMemory>>);
    const onOpen = vi.fn();
    render(
      <RecallResultsList
        results={[
          row({ folded: { count: 2, memory_ids: [OLDER_1, OLDER_2] } }),
        ]}
        loading={false}
        query="deploy"
        onOpen={onOpen}
      />,
    );
    expect(screen.getByText("folded:2")).toBeInTheDocument();
    expect(referenceMemory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "folded:2" }));
    const first = await screen.findByRole("button", { name: `Summary ${OLDER_1}` });
    const second = screen.getByRole("button", {
      name: `Summary ${OLDER_2}`,
    });
    expect(referenceMemory).toHaveBeenCalledWith(OLDER_1, expect.objectContaining({ recordAccess: false, signal: expect.any(AbortSignal) }));
    first.focus();
    expect(first).toHaveFocus();
    fireEvent.click(first);
    fireEvent.click(second);
    expect(onOpen).toHaveBeenNthCalledWith(1, OLDER_1);
    expect(onOpen).toHaveBeenNthCalledWith(2, OLDER_2);
    fireEvent.click(screen.getByRole("button", { name: "folded:2" }));
    fireEvent.click(screen.getByRole("button", { name: "folded:2" }));
    expect(referenceMemory).toHaveBeenCalledTimes(2);
  });

  it("keeps a readable detail link when a summary cannot be loaded", async () => {
    vi.mocked(referenceMemory).mockRejectedValue(new Error("unavailable"));
    const onOpen = vi.fn();
    render(<RecallResultsList results={[row({ folded: { count: 1, memory_ids: [OLDER_1] } })]} loading={false} query="deploy" onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "folded:1" }));
    fireEvent.click(await screen.findByRole("button", { name: "olderVersion:1" }));
    expect(onOpen).toHaveBeenCalledWith(OLDER_1);
    expect(screen.getByRole("status")).toHaveTextContent("summariesFailed");
  });

  it("opens older versions while another summary request is still pending", async () => {
    vi.mocked(referenceMemory).mockImplementation((id) => id === OLDER_1
      ? new Promise(() => {})
      : Promise.resolve({ summary: "Loaded summary" } as Awaited<ReturnType<typeof referenceMemory>>));
    const onOpen = vi.fn();
    render(<RecallResultsList results={[row({ folded: { count: 2, memory_ids: [OLDER_1, OLDER_2] } })]} loading={false} query="deploy" onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "folded:2" }));
    fireEvent.click(screen.getByRole("button", { name: "olderVersion:1" }));
    expect(onOpen).toHaveBeenCalledWith(OLDER_1);
    expect(await screen.findByRole("button", { name: "Loaded summary" })).toBeInTheDocument();
  });

  it("aborts pending summary loads when the list unmounts", async () => {
    vi.mocked(referenceMemory).mockReturnValue(new Promise(() => {}));
    const view = render(<RecallResultsList results={[row({ folded: { count: 2, memory_ids: [OLDER_1, OLDER_2] } })]} loading={false} query="deploy" onOpen={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "folded:2" }));
    await waitFor(() => expect(referenceMemory).toHaveBeenCalledTimes(2));
    const signals = vi.mocked(referenceMemory).mock.calls.map(([, options]) => options?.signal);
    expect(signals.every((signal) => signal && !signal.aborted)).toBe(true);
    view.unmount();
    expect(signals.every((signal) => signal?.aborted)).toBe(true);
  });

  it("keeps the folded count when the id list is capped", () => {
    render(
      <RecallResultsList
        results={[row({ folded: { count: 4, memory_ids: [] } })]}
        loading={false}
        query="deploy"
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getByText("folded:4")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^openFolded:/ })).toBeNull();
  });

  it("marks a superseded row and opens the newer memory", () => {
    const onOpen = vi.fn();
    render(
      <RecallResultsList
        results={[row({ memory_id: OLDER_1, superseded_by: NEWER })]}
        loading={false}
        query="deploy"
        onOpen={onOpen}
      />,
    );
    expect(screen.getByText("superseded")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "openNewer" }));
    expect(onOpen).toHaveBeenCalledWith(NEWER);
  });

  it("opens the row's own memory from its View button", () => {
    const onOpen = vi.fn();
    render(
      <RecallResultsList
        results={[row()]}
        loading={false}
        query="deploy"
        onOpen={onOpen}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "view" }));
    expect(onOpen).toHaveBeenCalledWith(NEWEST);
  });

  it("shows the empty state echoing the query when nothing matched", () => {
    render(
      <RecallResultsList
        results={[]}
        loading={false}
        query="nothing here"
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getByText("noResultsTitle")).toBeInTheDocument();
    expect(screen.getByText("noResultsDesc:nothing here")).toBeInTheDocument();
  });
});
