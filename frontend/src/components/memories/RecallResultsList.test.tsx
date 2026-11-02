/**
 * RecallResultsList (#2132): the Memories tab's recall-mode result rows.
 *
 * Covers a row without `folded` (no folded line), a row whose older versions
 * recall folded under it (count + one focusable button per folded id, each
 * opening that id), a row shadowed by a `supersedes` edge (badge + a button
 * opening the newer memory), the per-row View button and the empty state.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RecallResultsList } from "./RecallResultsList";
import type { RecallResultItem } from "@/lib/types/memory";

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
    expect(within(item).getByText("score:0.81")).toBeInTheDocument();
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

  it("shows the folded count and opens each folded memory by id", () => {
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
    const first = screen.getByRole("button", { name: `openFolded:${OLDER_1}` });
    const second = screen.getByRole("button", {
      name: `openFolded:${OLDER_2}`,
    });
    // Short id as the visible text; real buttons, so they take keyboard focus.
    expect(first).toHaveTextContent(OLDER_1.slice(0, 8));
    first.focus();
    expect(first).toHaveFocus();
    fireEvent.click(first);
    fireEvent.click(second);
    expect(onOpen).toHaveBeenNthCalledWith(1, OLDER_1);
    expect(onOpen).toHaveBeenNthCalledWith(2, OLDER_2);
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
