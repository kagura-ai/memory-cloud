/** Tests for delete-by-filter (#1941), with the real translator. */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BulkDeleteMemoriesDialog,
  pinCreatedBefore,
  toForgetBulkFilters,
} from "./BulkDeleteMemoriesDialog";
import en from "@/messages/en.json";

const forgetBulk = vi.fn();
vi.mock("@/lib/api/memory", () => ({
  forgetBulk: (p: unknown) => forgetBulk(p),
}));

function dialogInContext(
  contextId: string,
  onDeleted = vi.fn(),
  onPartial = vi.fn(),
) {
  return (
    <NextIntlClientProvider locale="en" messages={en}>
      <BulkDeleteMemoriesDialog
        contextId={contextId}
        open
        onOpenChange={vi.fn()}
        onDeleted={onDeleted}
        onPartial={onPartial}
      />
    </NextIntlClientProvider>
  );
}

function renderDialog(onDeleted = vi.fn(), onPartial = vi.fn()) {
  render(dialogInContext("ctx-1", onDeleted, onPartial));
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

describe("pinCreatedBefore", () => {
  it("keeps the earlier bound", () => {
    expect(pinCreatedBefore(undefined, "2026-01-02T00:00:00.000Z")).toBe(
      "2026-01-02T00:00:00.000Z",
    );
    expect(
      pinCreatedBefore("2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"),
    ).toBe("2026-01-01T00:00:00.000Z");
    expect(
      pinCreatedBefore("2026-03-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"),
    ).toBe("2026-01-02T00:00:00.000Z");
    expect(pinCreatedBefore("2026-03-01T00:00:00.000Z", null)).toBe(
      "2026-03-01T00:00:00.000Z",
    );
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
      created_before: expect.any(String),
      dry_run: true,
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete 12 memories" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(12));
    // The delete is pinned to rows created before the count was taken.
    const del = forgetBulk.mock.calls[1][0];
    expect(del).toMatchObject({
      context_id: "ctx-1",
      type: "note",
      dry_run: false,
    });
    expect(new Date(del.created_before).getTime()).toBeLessThanOrEqual(
      Date.now(),
    );
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

  it("requires a fresh count when filters change during the preview", async () => {
    let finishCount!: (response: { matched: number }) => void;
    forgetBulk.mockImplementationOnce(
      () => new Promise((resolve) => { finishCount = resolve; }),
    );
    renderDialog();
    const type = screen.getByLabelText("Type");
    fireEvent.change(type, { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    fireEvent.change(type, { target: { value: "decision" } });
    await act(async () => {
      finishCount({ matched: 1 });
    });

    expect(screen.queryByRole("button", { name: "Delete 1 memory" })).toBeNull();
    expect(forgetBulk).toHaveBeenCalledTimes(1);
    forgetBulk
      .mockResolvedValueOnce({ matched: 3 })
      .mockResolvedValueOnce({ deleted: 3 });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete 3 memories" }));
    await waitFor(() => expect(forgetBulk).toHaveBeenCalledTimes(3));
    expect(forgetBulk.mock.calls[1][0]).toMatchObject({
      dry_run: true,
      type: "decision",
      context_id: "ctx-1",
    });
    expect(forgetBulk.mock.calls[2][0]).toEqual({
      ...forgetBulk.mock.calls[1][0],
      dry_run: false,
    });
  });

  it("does not offer a pending preview from a previous context", async () => {
    let finishCount!: (response: { matched: number }) => void;
    forgetBulk.mockImplementationOnce(
      () => new Promise((resolve) => { finishCount = resolve; }),
    );
    const { rerender } = render(dialogInContext("ctx-1"));
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    rerender(dialogInContext("ctx-2"));
    await act(async () => {
      finishCount({ matched: 2 });
    });
    expect(screen.queryByRole("button", { name: "Delete 2 memories" })).toBeNull();
    expect(screen.getByRole("button", { name: "Count matches" })).toBeEnabled();
    expect(forgetBulk).toHaveBeenCalledTimes(1);
  });

  it("treats a future start date as zero matches at the preview cutoff", async () => {
    renderDialog();
    fireEvent.change(screen.getByLabelText("Created from"), {
      target: { value: "2999-01-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    expect(
      await screen.findByRole("button", { name: "Nothing to delete" }),
    ).toBeDisabled();
    expect(forgetBulk).not.toHaveBeenCalled();
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
        next_cursor: "c1",
      })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 2000,
        remaining: true,
        next_cursor: "c2",
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
    expect(forgetBulk.mock.calls[1][0].cursor).toBeUndefined();
    const target = { ...forgetBulk.mock.calls[0][0], dry_run: false };
    expect(forgetBulk.mock.calls[1][0]).toEqual(target);
    expect(forgetBulk.mock.calls[2][0]).toEqual({ ...target, cursor: "c1" });
    expect(forgetBulk.mock.calls[3][0]).toEqual({ ...target, cursor: "c2" });
  });

  it("keeps the dialog and the error when a later batch fails", async () => {
    forgetBulk
      .mockResolvedValueOnce({ dry_run: true, matched: 3000, deleted: null })
      .mockResolvedValueOnce({
        dry_run: false,
        matched: null,
        deleted: 2000,
        remaining: true,
        next_cursor: "c1",
      })
      .mockRejectedValueOnce(new Error("boom"));
    const onPartial = vi.fn();
    const onDeleted = renderDialog(vi.fn(), onPartial);
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Count matches" }));
    await screen.findByText("3,000 memories match.");
    fireEvent.click(
      screen.getByRole("button", { name: "Delete 3,000 memories" }),
    );
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(
      screen.getByText(
        "2,000 memories were deleted before the error. Count again to continue.",
      ),
    ).toBeInTheDocument();
    // No success path: the parent refreshes quietly instead.
    expect(onDeleted).not.toHaveBeenCalled();
    expect(onPartial).toHaveBeenCalledWith(2000);
    expect(screen.getByRole("button", { name: "Count matches" })).toBeEnabled();
    // The second request resumed at the first one's cursor.
    expect(forgetBulk.mock.calls[2][0]).toMatchObject({ cursor: "c1" });
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
