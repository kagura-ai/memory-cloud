/** Tests for delete-by-filter (#1941), with the real translator. */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BulkDeleteMemoriesDialog,
  toForgetBulkFilters,
} from "./BulkDeleteMemoriesDialog";
import en from "@/messages/en.json";

const forgetBulk = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  forgetBulk: (p: unknown) => forgetBulk(p),
}));

function renderDialog(onDeleted = vi.fn()) {
  render(
    <NextIntlClientProvider locale="en" messages={en}>
      <BulkDeleteMemoriesDialog
        contextId="ctx-1"
        open
        onOpenChange={vi.fn()}
        onDeleted={onDeleted}
      />
    </NextIntlClientProvider>,
  );
  return onDeleted;
}

beforeEach(() => forgetBulk.mockReset());

describe("toForgetBulkFilters", () => {
  it("is null when nothing is set", () => {
    expect(
      toForgetBulkFilters({ from: "", to: "", type: " ", tags: " , " }),
    ).toBeNull();
  });

  it("makes the end day inclusive and splits tags", () => {
    const f = toForgetBulkFilters({
      from: "2026-01-01",
      to: "2026-01-31",
      type: "note",
      tags: "a, b,,",
    });
    expect(f?.type).toBe("note");
    expect(f?.tags).toEqual(["a", "b"]);
    const after = new Date(f!.created_after!);
    const before = new Date(f!.created_before!);
    expect(before.getTime() - after.getTime()).toBe(31 * 24 * 3600 * 1000);
  });
});

describe("BulkDeleteMemoriesDialog", () => {
  it("refuses to count without a filter", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    expect(
      await screen.findByText("Set at least one filter."),
    ).toBeInTheDocument();
    expect(forgetBulk).not.toHaveBeenCalled();
  });

  it("dry-runs, shows the count, then deletes the same filter", async () => {
    forgetBulk
      .mockResolvedValueOnce({ dry_run: true, matched: 12, deleted: null })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 12,
        remaining: false,
      });
    const onDeleted = renderDialog();

    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    expect(await screen.findByText("12 memories match.")).toBeInTheDocument();
    expect(forgetBulk).toHaveBeenLastCalledWith({
      context_id: "ctx-1",
      type: "note",
      dry_run: true,
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete 12 memories" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(12));
    expect(forgetBulk).toHaveBeenLastCalledWith({
      context_id: "ctx-1",
      type: "note",
      dry_run: false,
    });
  });

  it("changing a filter discards the old count", async () => {
    forgetBulk.mockResolvedValueOnce({
      dry_run: true,
      matched: 3,
      deleted: null,
    });
    renderDialog();
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    await screen.findByText("3 memories match.");
    fireEvent.change(screen.getByLabelText("Tags (any of)"), {
      target: { value: "x" },
    });
    expect(screen.queryByText("3 memories match.")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Count matches" }),
    ).toBeInTheDocument();
  });

  it("cannot delete when nothing matches", async () => {
    forgetBulk.mockResolvedValueOnce({
      dry_run: true,
      matched: 0,
      deleted: null,
    });
    renderDialog();
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    expect(
      await screen.findByRole("button", { name: "Nothing to delete" }),
    ).toBeDisabled();
  });

  it("repeats the delete until nothing remains and reports the total", async () => {
    forgetBulk
      .mockResolvedValueOnce({ dry_run: true, matched: 4500, deleted: null })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 2000,
        remaining: true,
      })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 2000,
        remaining: true,
      })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 500,
        remaining: false,
      });
    const onDeleted = renderDialog();
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    await screen.findByText("4,500 memories match.");
    fireEvent.click(
      screen.getByRole("button", { name: "Delete 4,500 memories" }),
    );
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(4500));
    expect(forgetBulk).toHaveBeenCalledTimes(4);
  });

  it("refreshes with what was deleted when a later batch fails", async () => {
    forgetBulk
      .mockResolvedValueOnce({ dry_run: true, matched: 3000, deleted: null })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 2000,
        remaining: true,
      })
      .mockRejectedValueOnce(new Error("boom"));
    const onDeleted = renderDialog();
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    await screen.findByText("3,000 memories match.");
    fireEvent.click(
      screen.getByRole("button", { name: "Delete 3,000 memories" }),
    );
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(onDeleted).toHaveBeenCalledWith(2000);
  });

  it("stops when a request deletes nothing even if it says more remain", async () => {
    forgetBulk
      .mockResolvedValueOnce({ dry_run: true, matched: 10, deleted: null })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 0,
        remaining: true,
      });
    const onDeleted = renderDialog();
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    await screen.findByText("10 memories match.");
    fireEvent.click(screen.getByRole("button", { name: "Delete 10 memories" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(0));
    expect(forgetBulk).toHaveBeenCalledTimes(2);
  });
});
