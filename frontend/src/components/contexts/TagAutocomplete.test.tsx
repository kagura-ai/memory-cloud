/**
 * Tests for TagAutocomplete (#618): debounced prefix suggestions for the
 * trailing CSV token, insert-on-select, and exclusion of already-present tags.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const getContextTags = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContextTags: (...args: unknown[]) => getContextTags(...args),
}));

import { TagAutocomplete } from "./TagAutocomplete";

beforeEach(() => {
  getContextTags.mockReset();
});

describe("TagAutocomplete (#618)", () => {
  it("suggests for the trailing token and inserts the selection with a trailing comma", async () => {
    getContextTags.mockResolvedValue({
      context_id: "c1",
      total: 1,
      tags: [{ tag: "auth", count: 5 }],
    });
    const onChange = vi.fn();
    render(<TagAutocomplete contextId="c1" value="au" onChange={onChange} />);
    const option = await screen.findByRole(
      "option",
      { name: /auth/ },
      { timeout: 2000 },
    );
    fireEvent.mouseDown(option);
    expect(onChange).toHaveBeenCalledWith("auth, ");
  });

  it("excludes tags already present in the CSV value", async () => {
    getContextTags.mockResolvedValue({
      context_id: "c1",
      total: 2,
      tags: [
        { tag: "deploy", count: 4 },
        { tag: "auth", count: 5 },
      ],
    });
    render(
      <TagAutocomplete contextId="c1" value="auth, de" onChange={vi.fn()} />,
    );
    // "deploy" is suggested; "auth" (already in the CSV) is filtered out.
    await screen.findByRole("option", { name: /deploy/ }, { timeout: 2000 });
    expect(screen.queryByRole("option", { name: /auth/ })).toBeNull();
  });

  it("does not query or open the listbox for an empty trailing token", () => {
    vi.useFakeTimers();
    try {
      render(
        <TagAutocomplete contextId="c1" value="auth, " onChange={vi.fn()} />,
      );
      // Advance well past the debounce deterministically — no real sleep.
      vi.advanceTimersByTime(400);
      expect(getContextTags).not.toHaveBeenCalled();
      expect(screen.queryByRole("listbox")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
