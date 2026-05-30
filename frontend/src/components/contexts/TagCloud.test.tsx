/**
 * Tests for TagCloud (#618 base + #830 drill-down): tag buttons, click →
 * onTagClick (additive), selectedTags passed as with_tags, aria-pressed
 * removed, and the two empty states (no tags vs. drilled-down-to-nothing).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}:${JSON.stringify(vars)}` : key,
}));

// Avoid Radix Tabs context in the test harness.
vi.mock("@/components/ui/tabs", () => ({
  Tabs: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  TabsList: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  TabsTrigger: ({ children }: { children: React.ReactNode }) => (
    <button type="button">{children}</button>
  ),
}));

const getContextTags = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContextTags: (...args: unknown[]) => getContextTags(...args),
}));

import { TagCloud } from "./TagCloud";

beforeEach(() => {
  getContextTags.mockReset();
  getContextTags.mockResolvedValue({
    context_id: "c1",
    total: 3,
    tags: [
      { tag: "auth", count: 10 },
      { tag: "deploy", count: 3 },
      { tag: "ui", count: 1 },
    ],
  });
});

describe("TagCloud", () => {
  it("renders tag buttons and calls onTagClick with the tag on click (#618)", async () => {
    const onTagClick = vi.fn();
    render(
      <TagCloud contextId="c1" selectedTags={[]} onTagClick={onTagClick} />,
    );
    const authBtn = await screen.findByRole("button", { name: /"tag":"auth"/ });
    fireEvent.click(authBtn);
    expect(onTagClick).toHaveBeenCalledWith("auth");
  });

  it("passes selectedTags to the API as with_tags (#830)", async () => {
    render(
      <TagCloud
        contextId="c1"
        selectedTags={["auth", "deploy"]}
        onTagClick={vi.fn()}
      />,
    );
    await waitFor(() => expect(getContextTags).toHaveBeenCalled());
    expect(getContextTags).toHaveBeenCalledWith(
      "c1",
      expect.objectContaining({ withTags: ["auth", "deploy"] }),
    );
  });

  it("does NOT render aria-pressed on tag buttons (#830 — selected tags leave the cloud)", async () => {
    render(<TagCloud contextId="c1" selectedTags={[]} onTagClick={vi.fn()} />);
    const authBtn = await screen.findByRole("button", { name: /"tag":"auth"/ });
    expect(authBtn).not.toHaveAttribute("aria-pressed");
  });

  it("renders the base empty state when the context has no tags", async () => {
    getContextTags.mockResolvedValue({ context_id: "c1", total: 0, tags: [] });
    render(<TagCloud contextId="c1" selectedTags={[]} onTagClick={vi.fn()} />);
    expect(await screen.findByText("emptyTitle")).toBeInTheDocument();
  });

  it("renders the drill-down empty state when a filter leaves no co-occurring tags (#830)", async () => {
    getContextTags.mockResolvedValue({ context_id: "c1", total: 0, tags: [] });
    render(
      <TagCloud contextId="c1" selectedTags={["auth"]} onTagClick={vi.fn()} />,
    );
    expect(await screen.findByText("emptyRefinedTitle")).toBeInTheDocument();
  });
});
