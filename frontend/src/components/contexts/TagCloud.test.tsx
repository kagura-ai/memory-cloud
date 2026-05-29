/**
 * Tests for TagCloud (#618): tag buttons, click → onTagClick, active aria-pressed,
 * and the empty state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

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

describe("TagCloud (#618)", () => {
  it("renders tag buttons and calls onTagClick with the tag on click", async () => {
    const onTagClick = vi.fn();
    render(
      <TagCloud contextId="c1" activeTag={null} onTagClick={onTagClick} />,
    );
    const authBtn = await screen.findByRole("button", { name: /"tag":"auth"/ });
    fireEvent.click(authBtn);
    expect(onTagClick).toHaveBeenCalledWith("auth");
  });

  it("marks the active tag with aria-pressed=true", async () => {
    render(<TagCloud contextId="c1" activeTag="deploy" onTagClick={vi.fn()} />);
    const deployBtn = await screen.findByRole("button", {
      name: /"tag":"deploy"/,
    });
    expect(deployBtn).toHaveAttribute("aria-pressed", "true");
  });

  it("renders the empty state when the context has no tags", async () => {
    getContextTags.mockResolvedValue({ context_id: "c1", total: 0, tags: [] });
    render(<TagCloud contextId="c1" activeTag={null} onTagClick={vi.fn()} />);
    expect(await screen.findByText("emptyTitle")).toBeInTheDocument();
  });
});
