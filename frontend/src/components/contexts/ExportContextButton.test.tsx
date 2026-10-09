/** Tests for the context export download (#1941 surfaces the #950 export). */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExportContextButton, exportFileName } from "./ExportContextButton";

const exportContext = vi.fn();
const toast = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  exportContext: (id: string) => exportContext(id),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));

beforeEach(() => {
  exportContext.mockReset();
  toast.mockReset();
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
});

describe("exportFileName", () => {
  it("slugs the context name", () => {
    expect(exportFileName("My Notes/2026")).toBe("my-notes-2026-export.json");
    expect(exportFileName("日本語")).toBe("context-export.json");
  });
});

describe("ExportContextButton", () => {
  it("downloads the export as JSON", async () => {
    exportContext.mockResolvedValue({
      context: { name: "notes" },
      memories: [],
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    render(<ExportContextButton contextId="c1" contextName="notes" />);
    fireEvent.click(screen.getByRole("button", { name: "export" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(exportContext).toHaveBeenCalledWith("c1");
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
    click.mockRestore();
  });

  it("toasts when the export fails", async () => {
    exportContext.mockRejectedValue(new Error("413"));
    render(<ExportContextButton contextId="c1" contextName="notes" />);
    fireEvent.click(screen.getByRole("button", { name: "export" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "exportFailed",
          variant: "destructive",
        }),
      ),
    );
  });
});
